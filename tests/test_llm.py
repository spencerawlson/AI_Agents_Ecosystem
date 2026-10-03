"""Tests for real LLM inference wiring (litellm is mocked — no network)."""

import json
import sys
import types

import pytest

from agents.discovery.agent import DiscoveryAgent
from agents.discovery.llm_source import LLMOpportunitySource
from agents.research.agent import ResearchAgent
from agents.research.llm_source import LLMResearchSource
from core.allocation import CapitalAllocator
from core.intelligence import ModelRouter
from core.llm import LLMGateway, LLMUnavailable
from core.registry import BusinessRegistry
from orchestrator.models import Task

KEY_VARS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def _clear_keys(monkeypatch):
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def make_fake_litellm(response_json, cost=0.0012, fail=None):
    """Build a fake litellm module. `fail` = exception raised by completion."""
    mod = types.ModuleType("litellm")

    class _Usage:
        prompt_tokens = 120
        completion_tokens = 60

    class _Msg:
        content = json.dumps(response_json)

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    def completion(model, messages, **kwargs):
        completion.calls.append({"model": model, "messages": messages,
                                 "kwargs": kwargs})
        if fail is not None:
            raise fail
        return _Resp()

    completion.calls = []
    mod.completion = completion
    mod.completion_cost = lambda resp: cost  # noqa: E731
    return mod


def install_fake(monkeypatch, response_json=None, cost=0.0012, fail=None):
    mod = make_fake_litellm(response_json or {}, cost=cost, fail=fail)
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return mod


# -- gateway ------------------------------------------------------------

def test_gateway_complete_parses_json_and_cost(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    mod = install_fake(monkeypatch, {"opportunities": []}, cost=0.004)
    gw = LLMGateway()
    out = gw.complete('{"opportunities": []}', tier="cheap")
    assert out["json"] == {"opportunities": []}
    assert out["model"] == "gemini/gemini-2.5-flash"
    assert out["input_tokens"] == 120
    assert out["output_tokens"] == 60
    assert out["cost_usd"] == pytest.approx(0.004)
    # json_mode requests a JSON object response format
    assert mod.completion.calls[0]["kwargs"]["response_format"] == {
        "type": "json_object"}


def test_gateway_unavailable_without_key(monkeypatch):
    _clear_keys(monkeypatch)
    install_fake(monkeypatch, {})
    gw = LLMGateway()
    assert not LLMGateway.enabled()
    with pytest.raises(LLMUnavailable):
        gw.complete("hi")


def test_gateway_unavailable_without_litellm(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # sys.modules["litellm"] = None makes `import litellm` raise ImportError
    monkeypatch.setitem(sys.modules, "litellm", None)
    assert not LLMGateway.enabled()
    with pytest.raises(LLMUnavailable):
        LLMGateway().complete("hi")


def test_gateway_unavailable_on_api_error(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    install_fake(monkeypatch, {}, fail=RuntimeError("401 auth error"))
    assert LLMGateway.enabled()  # key present, import ok
    with pytest.raises(LLMUnavailable):
        LLMGateway().complete("hi")


def test_gateway_unavailable_on_bad_json(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    mod = make_fake_litellm({})

    def bad_completion(model, messages, **kwargs):
        resp = make_fake_litellm({}).completion(model, messages, **kwargs)
        resp.choices[0].message.content = "not json at all"
        return resp

    mod.completion = bad_completion
    monkeypatch.setitem(sys.modules, "litellm", mod)
    with pytest.raises(LLMUnavailable):
        LLMGateway().complete("hi")


def test_model_env_override(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ECOSYSTEM_CHEAP_MODEL", "openai/gpt-6-luna")
    gw = LLMGateway()
    assert gw.model_for("cheap") == "openai/gpt-6-luna"
    assert gw.model_for("smart") == "gemini/gemini-2.5-pro"


# -- router -------------------------------------------------------------

def test_router_route_real_maps_tiers(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    install_fake(monkeypatch, {})
    router = ModelRouter(gateway=LLMGateway())
    assert router.route_real("scan niches", 1) == "gemini/gemini-2.5-flash"
    assert router.route_real("score ideas", 2) == "gemini/gemini-2.5-flash"
    assert router.route_real("allocate capital", 3) == "gemini/gemini-2.5-pro"
    with pytest.raises(ValueError):
        router.route_real("x", 9)


def test_router_route_real_unavailable_without_gateway(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.delitem(sys.modules, "litellm", raising=False)
    router = ModelRouter()
    with pytest.raises(LLMUnavailable):
        router.route_real("scan niches", 1)
    # heuristic route still works as fallback
    assert router.route("scan niches", 1).name == "fast"


# -- sources ------------------------------------------------------------

OPPS_JSON = {
    "opportunities": [
        {"niche": "ai meal planners", "business_type": "ai_service",
         "one_liner": "weekly meal plans", "why_now": "health trend",
         "demand_score": 0.8, "competition_score": 0.4,
         "expected_margin": 0.85, "startup_cost_usd": 200,
         "advertising_cost_usd": 100, "operational_complexity": 0.3,
         "automation_potential": 0.9, "scalability": 0.9,
         "recurring_revenue": 0.7, "marketplace_risk": 0.1,
         "supplier_risk": 0.1, "support_burden": 0.2,
         "time_to_market_days": 14},
    ]
}


def test_llm_opportunity_source_success(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    install_fake(monkeypatch, OPPS_JSON, cost=0.002)
    src = LLMOpportunitySource(LLMGateway())
    opps = src.fetch({"limit": 5})
    assert len(opps) == 1
    assert opps[0]["niche"] == "ai meal planners"
    assert opps[0]["demand_score"] == pytest.approx(0.8)
    assert opps[0]["startup_cost_usd"] == pytest.approx(200.0)
    assert src.last_usage == {"tokens": 180, "cost_usd": pytest.approx(0.002)}


def test_llm_opportunity_source_falls_back(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    install_fake(monkeypatch, {}, fail=RuntimeError("rate limited"))
    src = LLMOpportunitySource(LLMGateway())
    opps = src.fetch({"limit": 3})
    assert len(opps) == 3  # heuristic niches
    assert src.last_usage is None


RESEARCH_JSON = {
    "opportunity_id": "opp_1", "niche": "ai meal planners",
    "business_type": "ai_service", "estimated_demand": "high",
    "competitor_count": 8, "top_competitors": ["a", "b"],
    "price_range_usd": [9.0, 29.0], "customer_profile": "busy parents",
    "advertising_competition": "medium", "marketplace_fees_pct": 0.08,
    "expected_margin": 0.85, "barriers_to_entry": ["trust"],
    "legal_constraints": [], "differentiation_angles": ["personalization"],
    "risks": ["churn"], "verdict": "pursue",
}


def test_llm_research_source_success_accumulates_usage(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    install_fake(monkeypatch, RESEARCH_JSON, cost=0.001)
    src = LLMResearchSource(LLMGateway())
    rep = src.research({"id": "opp_1", "niche": "ai meal planners",
                        "business_type": "ai_service"})
    assert rep["verdict"] == "pursue"
    assert rep["opportunity_id"] == "opp_1"
    src.research({"id": "opp_2", "niche": "x", "business_type": "y"})
    assert src.last_usage["tokens"] == 360  # 180 x 2 accumulated
    assert src.last_usage["cost_usd"] == pytest.approx(0.002)


def test_llm_research_source_falls_back(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    install_fake(monkeypatch, {}, fail=RuntimeError("timeout"))
    src = LLMResearchSource(LLMGateway())
    rep = src.research({"id": "opp_9", "niche": "notion templates",
                        "business_type": "digital_product"})
    assert rep["verdict"] in ("pursue", "watch", "reject")
    assert src.last_usage is None


# -- agents record real usage -------------------------------------------

def test_discovery_agent_records_real_llm_usage(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    install_fake(monkeypatch, OPPS_JSON, cost=0.002)
    agent = DiscoveryAgent(source=LLMOpportunitySource(LLMGateway()))
    task = Task(agent_type="discovery", inputs={"limit": 5},
                budget_usd=1.0, budget_tokens=5000)
    out = agent(task)
    assert out["count"] == 1
    # real usage, NOT the heuristic 200 tokens / $0.01 each
    assert agent.tokens_used == 180
    assert agent.cost_usd == pytest.approx(0.002)


def test_discovery_agent_heuristic_accounting_when_llm_down(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    install_fake(monkeypatch, {}, fail=RuntimeError("down"))
    agent = DiscoveryAgent(source=LLMOpportunitySource(LLMGateway()))
    task = Task(agent_type="discovery", inputs={"limit": 3},
                budget_usd=1.0, budget_tokens=5000)
    out = agent(task)
    assert out["count"] == 3
    assert agent.tokens_used == 600  # 200 x 3 heuristic
    assert agent.cost_usd == pytest.approx(0.03)


def test_research_agent_records_real_llm_usage(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    install_fake(monkeypatch, RESEARCH_JSON, cost=0.001)
    agent = ResearchAgent(source=LLMResearchSource(LLMGateway()))
    opps = [{"id": "opp_1", "niche": "ai meal planners",
             "business_type": "ai_service"}]
    task = Task(agent_type="research", inputs={"opportunities": opps},
                budget_usd=2.0, budget_tokens=8000)
    out = agent(task)
    assert out["count"] == 1
    assert agent.tokens_used == 180
    assert agent.cost_usd == pytest.approx(0.001)


# -- allocation smart rationale ------------------------------------------

def test_allocation_skips_rationale_when_llm_disabled(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.delitem(sys.modules, "litellm", raising=False)
    from core.ledger import Ledger
    registry, ledger = BusinessRegistry(), Ledger()
    registry.create("Test Biz", "digital_product")
    allocator = CapitalAllocator(registry, ledger)
    allocs = allocator.allocate(10_000, llm=LLMGateway())
    assert all(a.llm_rationale == "" for a in allocs)


def test_allocation_attaches_smart_rationale(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    install_fake(monkeypatch, {"rationale": "Scale: strong unit economics."},
                 cost=0.005)
    from core.ledger import Ledger
    registry, ledger = BusinessRegistry(), Ledger()
    biz = registry.create("Test Biz", "digital_product")
    ledger.record(business_id=biz.id, kind="revenue", amount_usd=5000,
                  description="sales")
    ledger.record(business_id=biz.id, kind="ai_spend", amount_usd=-100,
                  description="spend")
    allocator = CapitalAllocator(registry, ledger)
    allocs = allocator.allocate(10_000, llm=LLMGateway())
    assert allocs[0].llm_rationale == "Scale: strong unit economics."

"""Tests for the marketing mission: new agent actions with mocked Trends + LLM."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.creative.agent import CreativeAgent
from agents.marketing.agent import MarketingAgent
from core.market_data import MarketDataUnavailable
from orchestrator.models import Task


def _task(action, **inputs):
    return Task(agent_type="marketing", business_id="biz_1",
                inputs={"action": action, **inputs},
                budget_usd=10.0, budget_tokens=50000)


def _fake_trends(fail=False):
    client = MagicMock()

    def interest(kws):
        if fail:
            raise MarketDataUnavailable("no network")
        return {k: {"avg_12mo": 42.0, "trend": "rising", "latest": 55.0}
                for k in kws}

    client.interest.side_effect = interest
    return client


def _fake_llm_gateway(json_payload, input_tokens=100, output_tokens=200,
                      cost=0.02, enabled=True):
    gw_cls = MagicMock()
    gw_cls.enabled.return_value = enabled
    inst = MagicMock()
    inst.complete.return_value = {
        "text": "{}",
        "json": json_payload,
        "model": "test-model",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
    }
    gw_cls.return_value = inst
    return gw_cls


# -- seo_keyword_map ----------------------------------------------------

def test_seo_keyword_map_batches_three_per_call():
    seeds = [f"kw{i}" for i in range(12)]
    client = _fake_trends()
    with patch("core.market_data.TrendsClient", return_value=client):
        out = MarketingAgent()(_task("seo_keyword_map", niche="n", seeds=seeds))
    assert client.interest.call_count == 4
    for call in client.interest.call_args_list:
        assert len(call.args[0]) <= 3
    assert len(out["keywords"]) == 12
    assert all(k["trend"] == "rising" and k["avg_12mo"] == 42.0
               for k in out["keywords"])


def test_seo_keyword_map_marks_unavailable_on_failure():
    seeds = ["a", "b"]
    client = _fake_trends(fail=True)
    with patch("core.market_data.TrendsClient", return_value=client):
        out = MarketingAgent()(_task("seo_keyword_map", niche="n", seeds=seeds))
    assert all(k["trend"] == "unavailable" and k["avg_12mo"] is None
               for k in out["keywords"])


# -- content_calendar ----------------------------------------------------

def test_content_calendar_requires_llm():
    gw = _fake_llm_gateway({}, enabled=False)
    with patch("core.llm.LLMGateway", gw):
        with pytest.raises(RuntimeError, match="requires LLM mode"):
            MarketingAgent()(_task("content_calendar", niche="n",
                                   keyword_map={"keywords": []}, days=30))


def test_content_calendar_success():
    payload = {"calendar": [
        {"day": 1, "theme": "t", "format": "blog",
         "keyword": "k", "working_title": "title"}]}
    gw = _fake_llm_gateway(payload)
    with patch("core.llm.LLMGateway", gw):
        agent = MarketingAgent()
        out = agent(_task("content_calendar", niche="n",
                          keyword_map={"keywords": [{"keyword": "k"}]}, days=1))
    assert out["calendar"] == payload["calendar"]
    assert agent._cost_usd == pytest.approx(0.02)
    assert agent._tokens_used == 300


def test_positioning_brief_success():
    payload = {"audiences": ["a"], "value_props": ["v"],
               "content_angles": ["c"], "channels_ranked": ["x"]}
    gw = _fake_llm_gateway(payload)
    with patch("core.llm.LLMGateway", gw):
        out = MarketingAgent()(_task("positioning_brief", niche="n",
                                     site_url="https://x.test"))
    assert out["brief"] == payload


# -- social_drafts --------------------------------------------------------

def test_social_drafts_requires_llm():
    gw = _fake_llm_gateway({}, enabled=False)
    with patch("core.llm.LLMGateway", gw):
        with pytest.raises(RuntimeError, match="requires LLM mode"):
            CreativeAgent()(Task(agent_type="creative", business_id="biz_1",
                                 inputs={"action": "social_drafts"},
                                 budget_usd=10.0, budget_tokens=50000))


def test_social_drafts_creates_assets():
    drafts = [{"platform": "x", "text": f"post {i}", "hook": f"hook {i}"}
              for i in range(10)]
    gw = _fake_llm_gateway({"drafts": drafts})
    with patch("core.llm.LLMGateway", gw):
        agent = CreativeAgent()
        out = agent(Task(agent_type="creative", business_id="biz_1",
                         inputs={"action": "social_drafts", "niche": "n",
                                 "themes": ["t"], "count": 10},
                         budget_usd=10.0, budget_tokens=50000))
    assert len(out["drafts"]) == 10
    assert len(out["asset_ids"]) == 10
    assert len(agent._assets) == 10
    assert all(a.asset_type == "social" for a in agent._assets.values())
    assert out["drafts"][0]["platform"] == "x"

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


# -- peer-review evidence (regression: mission fail-stopped on the VM) ------

def _review_issues(output, criteria, agent_type="marketing"):
    from agents.review.agent import ReviewAgent
    task = Task(agent_type=agent_type, business_id="biz_1",
                inputs={}, budget_usd=10.0, budget_tokens=50000,
                acceptance_criteria=criteria)
    return ReviewAgent().review(task, output)


def test_peer_review_seo_keyword_map_evidences_no_invented_numbers():
    # Mission criterion that fail-stopped the VM run: the output satisfied it
    # semantically but never said so, so the reviewer flagged it.
    criteria = ["at least 9 keywords returned",
                "each keyword has avg_12mo and a trend value or 'unavailable'",
                "no invented numbers — gaps marked 'unavailable' explicitly"]
    with patch("core.market_data.TrendsClient", lambda: _fake_trends()):
        out = MarketingAgent()(_task(
            "seo_keyword_map", niche="n",
            seeds=[f"kw {i}" for i in range(12)]))
    issues = _review_issues(out, criteria)
    assert not [i for i in issues if "not evidenced in output" in i], issues
    assert out["provenance"]["no_invented_numbers"] is True
    assert out["provenance"]["gaps_explicitly_marked"] == "unavailable"


def test_peer_review_seo_keyword_map_with_real_gaps():
    # Even when every Trends call fails (all gaps), the criterion is evidenced.
    criteria = ["no invented numbers — gaps marked 'unavailable' explicitly"]
    with patch("core.market_data.TrendsClient",
               lambda: _fake_trends(fail=True)):
        out = MarketingAgent()(_task(
            "seo_keyword_map", niche="n", seeds=["kw 1", "kw 2"]))
    assert out["provenance"]["gaps_count"] == 2
    assert all(k["trend"] == "unavailable" for k in out["keywords"])
    issues = _review_issues(out, criteria)
    assert not [i for i in issues if "not evidenced in output" in i], issues


def test_peer_review_positioning_brief_evidenced():
    payload = {"audiences": ["a"], "value_props": ["v"],
               "content_angles": ["c"], "channels_ranked": ["x"]}
    gw = _fake_llm_gateway(payload)
    with patch("core.llm.LLMGateway", gw):
        out = MarketingAgent()(_task("positioning_brief", niche="n",
                                     site_url="https://x.test"))
    issues = _review_issues(
        out, ["audiences, value_props, content_angles, channels_ranked present"])
    assert not [i for i in issues if "not evidenced in output" in i], issues


def test_peer_review_content_calendar_evidenced():
    entries = [{"day": i + 1, "theme": "t", "format": "blog",
                "keyword": "k", "working_title": "w"} for i in range(30)]
    gw = _fake_llm_gateway({"calendar": entries})
    with patch("core.llm.LLMGateway", gw):
        out = MarketingAgent()(_task("content_calendar", niche="n", days=30))
    issues = _review_issues(
        out, ["30 calendar entries",
              "each entry has day, theme, format, keyword, working_title"])
    assert not [i for i in issues if "not evidenced in output" in i], issues


def test_peer_review_social_drafts_evidenced():
    drafts = [{"platform": "x", "text": f"post {i}", "hook": f"hook {i}"}
              for i in range(10)]
    gw = _fake_llm_gateway({"drafts": drafts})
    with patch("core.llm.LLMGateway", gw):
        out = CreativeAgent()(Task(
            agent_type="creative", business_id="biz_1",
            inputs={"action": "social_drafts", "niche": "n",
                    "themes": ["t"], "count": 10},
            budget_usd=10.0, budget_tokens=50000))
    issues = _review_issues(
        out, ["10 drafts returned", "each draft has platform, text, hook"],
        agent_type="creative")
    assert not [i for i in issues if "not evidenced in output" in i], issues


# -- fail-fast LLM diagnosis ----------------------------------------------

def test_require_llm_ready_passes_when_enabled():
    from ecosystem.marketing_mission import _require_llm_ready
    with patch("core.llm.LLMGateway.enabled", return_value=True):
        _require_llm_ready()  # must not raise


def test_require_llm_ready_fails_fast_with_diagnosis():
    import builtins

    from ecosystem.marketing_mission import _require_llm_ready
    real_import = builtins.__import__

    def no_litellm(name, *args, **kwargs):
        if name == "litellm":
            raise ImportError("no litellm")
        return real_import(name, *args, **kwargs)

    with patch("core.llm.LLMGateway.enabled", return_value=False), \
         patch("builtins.__import__", side_effect=no_litellm), \
         patch.dict("os.environ", {}, clear=False):
        # ensure no key leaks in from the real environment
        import os
        for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            os.environ.pop(k, None)
        with pytest.raises(SystemExit) as exc:
            _require_llm_ready()
    msg = str(exc.value)
    assert "litellm" in msg and "OPENAI_API_KEY" in msg, msg

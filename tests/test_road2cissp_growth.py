"""Road to CISSP growth program: site audit, strategy/content agent actions,
and the full cycle (propose → approve → draft → review) with no network."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.creative.agent import CreativeAgent
from agents.marketing.agent import MarketingAgent
from core.site_audit import SiteAuditor, parse_page
from ecosystem import road2cissp_growth as growth
from ecosystem.runtime import build_runtime
from orchestrator.models import Task

SHELL = ("<html><head><title>Road to CISSP</title>"
         "<meta name='description' content='Free CISSP prep'></head>"
         "<body><div id='root'></div><script src='/main.js'></script></body></html>")
SITEMAP = ("<urlset><url><loc>https://road2cissp.com/</loc></url>"
           "<url><loc>https://road2cissp.com/labs</loc></url>"
           "<url><loc>https://road2cissp.com/practice</loc></url></urlset>")


def spa_fetch(url):
    if url.endswith("robots.txt"):
        return 200, "User-agent: *\nAllow: /"
    if url.endswith("sitemap.xml"):
        return 200, SITEMAP
    return 200, SHELL


def good_fetch(url):
    if url.endswith("robots.txt"):
        return 200, "User-agent: *\nAllow: /\nSitemap: https://road2cissp.com/sitemap.xml"
    if url.endswith("sitemap.xml"):
        return 200, SITEMAP
    name = url.rstrip("/").rsplit("/", 1)[-1]
    words = " ".join(["cissp"] * 200)
    return 200, (f"<html><head><title>{name} | Road to CISSP</title>"
                 f"<meta name='description' content='About {name}'>"
                 f"<meta property='og:image' content='/og.png'>"
                 f"<link rel='canonical' href='{url}'>"
                 "<script defer src='https://plausible.io/js/script.js'></script>"
                 f"</head><body><h1>{name}</h1><p>{words}</p></body></html>")


# -- site audit -----------------------------------------------------------

def test_audit_flags_spa_shell_and_missing_analytics():
    r = SiteAuditor(fetch=spa_fetch).audit("https://road2cissp.com")
    issues = [f["issue"] for f in r["findings"] if f["severity"] == "critical"]
    assert any("client-rendered shell" in i for i in issues)
    assert any("no analytics" in i for i in issues)
    assert r["pages_checked"] == 3
    assert r["findings"][0]["severity"] == "critical"  # sorted by severity


def test_audit_clean_site_has_no_critical_findings():
    r = SiteAuditor(fetch=good_fetch).audit("https://road2cissp.com")
    assert r["severity_counts"]["critical"] == 0
    assert r["severity_counts"]["high"] == 0


def test_audit_unreachable_site_is_critical_not_crash():
    r = SiteAuditor(fetch=lambda u: (0, "")).audit("https://road2cissp.com")
    assert any("could not be fetched" in f["issue"] for f in r["findings"])


def test_parse_page_ignores_script_text_in_word_count():
    p = parse_page("<title>T</title><script>var a = 'lots of words here';</script>"
                   "<p>two words</p>")
    assert p["word_count"] == 2


# -- fake LLM -------------------------------------------------------------

STRATEGY = {
    "summary": "Fix crawlability first, then win r/cissp with genuinely helpful answers...",
    "north_star": {"metric": "weekly sign-ins", "current": None,
                   "target_90d": 150, "why": "sign-ins mean real learners"},
    "diagnosis": ["Google sees one page", "nothing is measured"],
    "channel_bets": [
        {"channel": "SEO", "rationale": "high intent", "impact": 9,
         "confidence": 6, "ease": 4},
        {"channel": "Reddit", "rationale": "active community", "impact": 7,
         "confidence": 7, "ease": 8},
    ],
    "experiments": [
        {"slug": "reddit-answers", "name": "Reddit answers", "channel": "community",
         "hypothesis": "helpful answers drive visits", "actions": ["answer 10 questions"],
         "kpi": "referral visits", "target": "100 visits", "duration_days": 14,
         "budget_usd": 0, "stop_rule": "<10 visits after 7 days",
         "content_needed": ["community", "bogus"]},
        {"slug": "Reddit Answers", "name": "duplicate", "hypothesis": "dup"},
        {"name": "Domain explainers", "channel": "SEO", "hypothesis": "posts rank",
         "actions": ["publish 4 posts"], "kpi": "impressions", "target": "1k",
         "duration_days": 500, "budget_usd": "abc", "stop_rule": "no impressions",
         "content_needed": ["blog"]},
    ],
    "site_fixes": [{"fix": "prerender routes", "why": "crawlability", "priority": "P0"}],
    "this_week": ["Install Plausible", "Verify Search Console"],
}

CONTENT = {"pieces": [
    {"type": "community", "venue": "r/cissp", "context": "how to study domain 3",
     "text": "Start with the security models..."},
    {"type": "community", "venue": "r/cissp"},  # missing text → dropped
    {"type": "blog", "title": "not requested", "body_markdown": "x"},  # wrong type
]}


def fake_gateway(enabled=True, fail_strategy=False):
    cls = MagicMock()
    cls.enabled.return_value = enabled
    inst = MagicMock()

    def complete(prompt, **kw):
        if "head of growth" in prompt:
            if fail_strategy:
                from core.llm import LLMUnavailable
                raise LLMUnavailable("quota exceeded")
            payload = STRATEGY
        else:
            payload = CONTENT
        return {"text": "{}", "json": payload, "model": "m", "input_tokens": 100,
                "output_tokens": 200, "cost_usd": 0.01}

    inst.complete.side_effect = complete
    cls.return_value = inst
    return cls


def _task(agent, action, **inputs):
    return Task(agent_type=agent, business_id="biz_1",
                inputs={"action": action, **inputs},
                budget_usd=5.0, budget_tokens=50000)


# -- agent actions ----------------------------------------------------------

def test_growth_strategy_normalises_llm_output():
    with patch("core.llm.LLMGateway", fake_gateway()):
        out = MarketingAgent()(_task("marketing", "growth_strategy",
                                     profile={"site_url": "https://road2cissp.com"}))
    s = out["strategy"]
    assert [b["channel"] for b in s["channel_bets"]] == ["Reddit", "SEO"]  # ICE order
    assert all("ice" in b for b in s["channel_bets"])
    slugs = [e["slug"] for e in s["experiments"]]
    assert slugs == ["reddit-answers", "domain-explainers"]  # dup slug dropped
    assert s["experiments"][0]["content_needed"] == ["community"]
    assert s["experiments"][1]["duration_days"] == 90
    assert s["experiments"][1]["budget_usd"] == 0.0
    assert "..." not in s["summary"]


def test_growth_strategy_requires_llm():
    with patch("core.llm.LLMGateway", fake_gateway(enabled=False)):
        with pytest.raises(RuntimeError, match="requires LLM"):
            MarketingAgent()(_task("marketing", "growth_strategy"))


def test_content_pieces_keeps_only_valid_requested_types():
    agent = CreativeAgent()
    exp = {"slug": "reddit-answers", "name": "Reddit", "content_needed": ["community"]}
    with patch("core.llm.LLMGateway", fake_gateway()):
        out = agent(_task("creative", "content_pieces", experiment=exp))
    assert len(out["pieces"]) == 1
    assert out["pieces"][0]["venue"] == "r/cissp"
    assert agent.run(_task("creative", "report"))["total_assets"] == 1


# -- full cycle -------------------------------------------------------------

def _cycle(rt, tmp_path, now, gw=None, **kw):
    gw = gw or fake_gateway()
    trends = MagicMock()
    trends.interest.side_effect = lambda kws: {
        k: {"avg_12mo": 30.0, "trend": "rising", "latest": 35.0} for k in kws}
    with patch("core.llm.LLMGateway", gw), \
         patch("core.site_audit.http_fetch", spa_fetch), \
         patch("core.market_data.TrendsClient", return_value=trends):
        return growth.run_growth_cycle(
            rt, state_path=tmp_path / "state.json", reports_root=tmp_path / "reports",
            now=now, llm_enabled=kw.pop("llm_enabled", True), **kw)


def test_full_cycle_propose_approve_draft_review(tmp_path, monkeypatch):
    monkeypatch.delenv("PLAUSIBLE_API_KEY", raising=False)
    rt = build_runtime(use_llm=False)
    t0 = datetime(2026, 10, 6, tzinfo=timezone.utc)

    s1 = _cycle(rt, tmp_path, t0)
    assert not s1["errors"], s1["errors"]
    assert {r.split(":")[0] for r in s1["ran"]} == {"audit", "keywords", "strategy"}
    pending = [a for a in rt.approvals.pending() if a.action == "marketing_experiment"]
    assert len(pending) == 2
    report = Path(s1["report"]).read_text(encoding="utf-8")
    assert "NOTHING WAS PUBLISHED" in report and "client-rendered shell" in report

    # Nothing is due an hour later, and nothing is re-proposed.
    s2 = _cycle(rt, tmp_path, t0 + timedelta(hours=1))
    assert s2["ran"] == [] and s2["report"] is None
    assert len(rt.approvals.pending()) == 2

    reddit = next(a for a in pending if a.details["slug"] == "reddit-answers")
    seo = next(a for a in pending if a.details["slug"] == "domain-explainers")
    rt.approvals.decide(reddit.id, True, "owner")
    rt.approvals.decide(seo.id, False, "owner", note="later")

    s3 = _cycle(rt, tmp_path, t0 + timedelta(hours=2))
    assert "experiment approved → active: reddit-answers" in s3["events"]
    assert "experiment rejected: domain-explainers" in s3["events"]
    drafts = list((tmp_path / "reports" / "road2cissp_drafts" / "reddit-answers").glob("*.md"))
    assert len(drafts) == 1
    assert "DRAFT — not published" in drafts[0].read_text(encoding="utf-8")

    s4 = _cycle(rt, tmp_path, t0 + timedelta(days=6, hours=23))
    assert "experiment finished, awaiting outcome: reddit-answers" not in s4["events"]
    s5 = _cycle(rt, tmp_path, t0 + timedelta(days=16))
    assert "experiment finished, awaiting outcome: reddit-answers" in s5["events"]

    state = growth.load_state(tmp_path / "state.json")
    assert state["experiments"]["reddit-answers"]["status"] == "review"
    assert state["strategy"]["cycle"] == 2  # weekly re-plan happened on day 16
    assert state["spend_usd"] > 0
    # The re-plan proposed the same slugs: neither the active nor the
    # owner-rejected experiment is asked about again.
    assert not [a for a in rt.approvals.pending() if a.action == "marketing_experiment"]


def test_strategy_failure_backs_off_instead_of_respending(tmp_path, monkeypatch):
    monkeypatch.delenv("PLAUSIBLE_API_KEY", raising=False)
    rt = build_runtime(use_llm=False)
    t0 = datetime(2026, 10, 6, tzinfo=timezone.utc)
    gw = fake_gateway(fail_strategy=True)
    s1 = _cycle(rt, tmp_path, t0, gw=gw)
    assert any(e.startswith("strategy:") for e in s1["errors"])
    s2 = _cycle(rt, tmp_path, t0 + timedelta(minutes=5), gw=gw)
    assert s2["errors"] == []  # backed off
    s3 = _cycle(rt, tmp_path, t0 + timedelta(hours=7), gw=gw)
    assert any(e.startswith("strategy:") for e in s3["errors"])  # retried


def test_without_llm_research_still_runs_and_strategy_is_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("PLAUSIBLE_API_KEY", raising=False)
    rt = build_runtime(use_llm=False)
    s = _cycle(rt, tmp_path, datetime(2026, 10, 6, tzinfo=timezone.utc),
               llm_enabled=False)
    assert {r.split(":")[0] for r in s["ran"]} == {"audit", "keywords"}
    assert any("strategy" in x for x in s["skipped"])


def test_cli_record_metrics_and_outcome(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(growth, "default_state_path", lambda: tmp_path / "s.json")
    growth.save_state(tmp_path / "s.json", {"experiments": {"x": {
        "name": "X", "status": "review", "duration_days": 14, "budget_usd": 0}}})
    assert growth.main(["record-metrics", "--visitors", "420", "--signups", "12"]) == 0
    assert growth.main(["outcome", "x", "--worked", "60 visits from Reddit"]) == 0
    state = growth.load_state(tmp_path / "s.json")
    assert state["metrics"][-1]["visitors"] == 420
    assert state["experiments"]["x"]["status"] == "completed"
    assert "worked" in state["experiments"]["x"]["outcome"]


# -- worker focus -----------------------------------------------------------

def test_worker_defaults_to_growth_focus_only():
    from ecosystem import worker

    rt = build_runtime(use_llm=False)
    with patch.object(worker, "run_growth_tick") as g, \
         patch.object(worker, "run_tick") as d, \
         patch.object(worker, "run_store_tick") as s:
        worker.main(["--ticks", "1", "--interval", "0", "--monitor-every", "0"], rt=rt)
    assert g.called and not d.called and not s.called


def test_worker_opt_in_discovery_and_store():
    from ecosystem import worker

    rt = build_runtime(use_llm=False)
    with patch.object(worker, "run_growth_tick") as g, \
         patch.object(worker, "run_tick", return_value={"ok": False}) as d, \
         patch.object(worker, "run_store_tick") as s:
        worker.main(["--ticks", "1", "--interval", "0", "--monitor-every", "0",
                     "--no-growth", "--discovery", "--store"], rt=rt)
    assert not g.called and d.called and s.called

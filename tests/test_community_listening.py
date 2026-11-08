"""Community listening: Reddit search, intent scoring, the 90/10 draft
gate, reply drafting, the listen mission and its daily-plan feed.

No network, no real LLM, no spend, no posting anywhere in this file —
the listener runs against fake fetchers, the agent against a fake
gateway, and the mission against a patched listener.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.community_listening import (
    REPLY_THRESHOLD, SourceUnavailable, fetch_subreddit_rules,
    filter_threads, listen_reddit, parse_listing, score_thread,
    search_reddit, validate_reply_draft,
)
from ecosystem import daily_plan
from businesses.road2cissp.growth import CADENCE, RETRY_AFTER
import businesses.road2cissp.growth as growth

SITE = "https://road2cissp.com"
NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
KEYWORDS = ["cissp study plan", "how to pass cissp"]


def _listing(*items):
    return {"data": {"children": [
        {"data": {"id": i["id"], "subreddit": i.get("subreddit", "cissp"),
                  "title": i["title"], "selftext": i.get("text", ""),
                  "permalink": f"/r/cissp/comments/{i['id']}/x/",
                  "author": i.get("author", "learner1"),
                  "score": i.get("score", 3),
                  "num_comments": i.get("num_comments", 0),
                  "created_utc": i.get("created", NOW.timestamp()),
                  "over_18": i.get("over_18", False),
                  "locked": i.get("locked", False)}}
        for i in items]}}


def _fake_fetch(payload, status=200):
    def fetch(url):
        fetch.last_url = url
        return status, json.dumps(payload)
    return fetch


GOOD_HELP = ("Start with the official outline, then do one domain a week "
             "with flashcards and practice questions. I did that for "
             "domain 3 and the models finally clicked after I drew them "
             "out on paper and quizzed myself every morning.")
GOOD_REPLY = GOOD_HELP + " I built road2cissp.com to organise exactly that."


# -- parsing and search -----------------------------------------------------

def test_parse_listing_builds_thread_dicts():
    threads = parse_listing(_listing(
        {"id": "abc", "title": "How do I study?", "text": "Stuck on domain 3"}))
    assert len(threads) == 1
    t = threads[0]
    assert t["venue"] == "r/cissp"
    assert t["url"] == "https://www.reddit.com/r/cissp/comments/abc/x/"
    assert t["text"] == "Stuck on domain 3"


def test_search_reddit_queries_one_subreddit_past_week():
    fetch = _fake_fetch(_listing({"id": "a1", "title": "q?"}))
    found = search_reddit("r/cissp", "cissp study plan", fetch=fetch)
    assert [t["id"] for t in found] == ["a1"]
    url = fetch.last_url
    assert "r/cissp/search.json" in url
    assert "restrict_sr=1" in url and "t=week" in url
    assert "cissp+study+plan" in url or "cissp%20study%20plan" in url


def test_search_reddit_blocked_page_is_unavailable_not_empty():
    blocked = ("<title>Blocked</title><h1>whoa there, pardner!</h1>")
    with pytest.raises(SourceUnavailable, match="blocked"):
        search_reddit("r/cissp", "cissp", fetch=lambda u: (200, blocked))
    with pytest.raises(SourceUnavailable, match="blocked"):
        search_reddit("r/cissp", "cissp", fetch=lambda u: (403, ""))
    with pytest.raises(SourceUnavailable, match="network failure"):
        search_reddit("r/cissp", "cissp", fetch=lambda u: (0, ""))
    with pytest.raises(SourceUnavailable, match="HTTP 500"):
        search_reddit("r/cissp", "cissp", fetch=lambda u: (500, ""))


def test_fetch_subreddit_rules_ok_and_failure():
    payload = {"data": {"rules": [
        {"short_name": "No self-promotion",
         "description": "No links to your own course."}]}}
    rules = fetch_subreddit_rules("r/cissp", fetch=_fake_fetch(payload))
    assert rules[0]["short_name"] == "No self-promotion"
    assert fetch_subreddit_rules("r/cissp", fetch=lambda u: (0, "")) == []
    assert fetch_subreddit_rules("r/cissp",
                                 fetch=lambda u: (200, "not json")) == []


# -- scoring ------------------------------------------------------------------

def test_score_thread_high_for_question_with_problem_and_decision():
    thread = {"title": "How do I pass CISSP? Need a cissp study plan",
              "text": "I am struggling and confused. Which course is best, "
                      "should I buy the official guide or use a cissp study plan?",
              "num_comments": 8}
    score, reasons = score_thread(thread, KEYWORDS)
    assert score >= 75
    assert "asks a question" in reasons
    assert "states a problem / asks for help" in reasons
    assert "deciding between options" in reasons
    assert any("keyword in title" in r for r in reasons)


def test_score_thread_promo_is_penalised_and_body_keyword_is_small():
    thread = {"title": "50% off my CISSP course — discount code inside",
              "text": "Buy now, check out my course for a cissp study plan.",
              "num_comments": 0}
    score, _ = score_thread(thread, KEYWORDS)
    assert score < REPLY_THRESHOLD


def test_filter_threads_drops_nsfw_locked_deleted_and_old():
    old = (NOW - timedelta(days=20)).timestamp()
    raw = parse_listing(_listing(
        {"id": "good", "title": "How do I study?",
         "text": "Stuck, any advice?", "num_comments": 4},
        {"id": "nsfw", "title": "x", "over_18": True},
        {"id": "locked", "title": "x", "locked": True},
        {"id": "gone", "title": "x", "author": "[deleted]"},
        {"id": "old", "title": "x", "created": old},
        {"id": "good", "title": "How do I study?"}))  # duplicate id
    kept, dropped = filter_threads(raw, KEYWORDS, NOW)
    assert [t["id"] for t in kept] == ["good"]
    assert sorted(d["drop_reason"] for d in dropped) == [
        "author gone", "duplicate", "locked", "nsfw", "older than 14 days"]
    assert kept[0]["intent"] in ("worth_reply", "high")


# -- the 90/10 gate -----------------------------------------------------------

def test_validate_reply_draft_valid_passes():
    assert validate_reply_draft(GOOD_REPLY, SITE) == []


def test_validate_reply_draft_rejects_each_violation():
    assert any("disclosure" in e for e in
               validate_reply_draft(GOOD_HELP + " See road2cissp.com.", SITE))
    assert any("not at the end" in e for e in validate_reply_draft(
        "I built road2cissp.com. " + GOOD_HELP, SITE))
    assert any("promotional" in e for e in validate_reply_draft(
        GOOD_HELP + " I built road2cissp.com, buy now with a discount!", SITE))
    assert any("200 characters" in e for e in validate_reply_draft(
        "Short help. I built road2cissp.com.", SITE))
    assert any("max 1" in e for e in validate_reply_draft(
        GOOD_HELP + " I built road2cissp.com and https://example.com too.",
        SITE))
    assert any("does not mention" in e for e in
               validate_reply_draft(GOOD_HELP, SITE))
    assert validate_reply_draft("", SITE) == ["empty reply"]


# -- orchestration --------------------------------------------------------------

def test_listen_reddit_interleaves_and_reports_sources():
    seen = []

    def fetch(url):
        seen.append(url)
        return 200, json.dumps(_listing(
            {"id": f"t{len(seen)}", "title": "How do I study cissp?",
             "text": "Stuck, any advice on a cissp study plan?",
             "num_comments": 5}))
    out = listen_reddit(["r/cissp", "r/CompTIA"], KEYWORDS, fetch=fetch,
                        limit_per_query=5, max_queries=4)
    assert out["sources"]["reddit"]["status"] == "ok"
    assert out["sources"]["reddit"]["queries"] == 4
    # Every subreddit was searched before any second keyword.
    subs = [u.split("/r/")[1].split("/")[0] for u in seen]
    assert subs == ["cissp", "CompTIA", "cissp", "CompTIA"]
    assert out["sources"]["x"]["status"] == "unavailable"
    assert out["sources"]["threads"]["status"] == "unavailable"
    assert all(t["intent_score"] >= REPLY_THRESHOLD for t in out["threads"])


def test_listen_reddit_block_stops_queries_and_reports_reason():
    calls = []

    def fetch(url):
        calls.append(url)
        return 200, "<h1>whoa there, pardner!</h1>"
    out = listen_reddit(["r/cissp", "r/CompTIA"], KEYWORDS, fetch=fetch)
    assert out["sources"]["reddit"]["status"] == "unavailable"
    assert "blocked" in out["sources"]["reddit"]["reason"]
    assert out["threads"] == []
    assert len(calls) == 1  # no point burning more calls against a block


# -- creative agent -------------------------------------------------------------

def _reply_gateway(replies):
    cls = MagicMock()
    cls.enabled.return_value = True
    cls.SMART = "smart"
    inst = MagicMock()
    inst.complete.return_value = {
        "text": "{}", "json": {"replies": replies}, "model": "fake",
        "input_tokens": 100, "output_tokens": 200, "cost_usd": 0.01}
    cls.return_value = inst
    return cls


def test_community_replies_keeps_valid_and_drops_gate_failures():
    from agents.creative.agent import CreativeAgent
    from orchestrator.models import Task

    threads = [
        {"id": "a1", "venue": "r/cissp", "title": "How do I study?",
         "text": "Stuck on domain 3, any advice?", "url": "https://x/a1",
         "intent_score": 85, "reasons": ["asks a question"]},
        {"id": "b2", "venue": "r/cissp", "title": "Best course?",
         "text": "Which course is best?", "url": "https://x/b2",
         "intent_score": 70, "reasons": ["deciding between options"]},
    ]
    replies = [{"thread_id": "a1", "text": GOOD_REPLY},
               {"thread_id": "b2", "text": "Buy my course now! Discount!"}]
    task = Task(agent_type="creative", business_id="biz_1",
                inputs={"action": "community_replies", "threads": threads,
                        "profile": {"site_url": SITE}, "site_url": SITE,
                        "facts": ["Road to CISSP is a study platform"]},
                budget_usd=5.0, budget_tokens=50000)
    with patch("core.llm.LLMGateway", _reply_gateway(replies)):
        out = CreativeAgent()(task)
    assert [r["thread_id"] for r in out["replies"]] == ["a1"]
    assert out["replies"][0]["asset_id"]
    assert len(out["dropped"]) == 1
    assert out["dropped"][0]["thread_id"] == "b2"
    assert "nothing posted" in out["evidence"]


def test_community_replies_requires_llm():
    from agents.creative.agent import CreativeAgent
    from orchestrator.models import Task

    task = Task(agent_type="creative", business_id="biz_1",
                inputs={"action": "community_replies",
                        "threads": [{"id": "a1", "text": "x"}]},
                budget_usd=5.0, budget_tokens=50000)
    cls = MagicMock()
    cls.enabled.return_value = False
    with patch("core.llm.LLMGateway", cls):
        with pytest.raises(RuntimeError, match="requires LLM"):
            CreativeAgent()(task)


# -- the listen mission ---------------------------------------------------------

def _listen_result():
    return {
        "threads": [
            {"id": "a1", "source": "reddit", "venue": "r/cissp",
             "subreddit": "cissp", "title": "How do I study?",
             "text": "Stuck on domain 3, any advice?",
             "url": "https://www.reddit.com/r/cissp/comments/a1/x/",
             "author": "learner1", "score": 5, "num_comments": 6,
             "created_utc": NOW.timestamp(), "over_18": False,
             "locked": False, "intent_score": 85, "intent": "high",
             "reasons": ["asks a question"]},
        ],
        "dropped": [],
        "sources": {
            "reddit": {"status": "ok", "queries": 2, "raw_found": 1},
            "x": {"status": "unavailable", "reason": "needs API credentials"},
            "threads": {"status": "unavailable",
                        "reason": "needs API credentials"},
        },
    }


def test_listen_mission_drafts_and_records_without_posting(tmp_path):
    from ecosystem.runtime import build_runtime

    rt = build_runtime(use_llm=True)
    with patch("core.community_listening.listen_reddit",
               return_value=_listen_result()), \
         patch("core.llm.LLMGateway",
               _reply_gateway([{"thread_id": "a1", "text": GOOD_REPLY}])):
        summary = growth.run_listen(
            rt, state_path=tmp_path / "state.json",
            reports_root=tmp_path / "reports", now=NOW)
    assert not summary["errors"], summary["errors"]
    assert summary["drafted"] == ["a1"]
    assert summary["spend_usd"] == pytest.approx(0.01)
    state = growth.load_state(tmp_path / "state.json")
    thread = state["community_threads"]["a1"]
    assert thread["status"] == "drafted"
    assert Path(thread["draft_path"]).exists()
    assert "NOTHING WAS POSTED" in Path(summary["report"]).read_text(encoding="utf-8")
    assert "reddit" in Path(summary["report"]).read_text(encoding="utf-8")

    # A second run finds the same thread and drafts nothing new.
    with patch("core.community_listening.listen_reddit",
               return_value=_listen_result()), \
         patch("core.llm.LLMGateway",
               _reply_gateway([{"thread_id": "a1", "text": GOOD_REPLY}])):
        again = growth.run_listen(
            rt, state_path=tmp_path / "state.json",
            reports_root=tmp_path / "reports", now=NOW)
    assert again["drafted"] == []


def test_listen_mission_reports_unavailable_reddit_without_spending(tmp_path):
    from ecosystem.runtime import build_runtime

    rt = build_runtime(use_llm=False)
    blocked = {"threads": [], "dropped": [],
               "sources": {
                   "reddit": {"status": "unavailable",
                              "reason": "blocked — run from FedServ",
                              "queries": 0, "raw_found": 0},
                   "x": {"status": "unavailable",
                         "reason": "needs API credentials"},
                   "threads": {"status": "unavailable",
                               "reason": "needs API credentials"}}}
    with patch("core.community_listening.listen_reddit",
               return_value=blocked):
        summary = growth.run_listen(
            rt, state_path=tmp_path / "state.json",
            reports_root=tmp_path / "reports", now=NOW)
    assert summary["errors"] == ["reddit: blocked — run from FedServ"]
    assert summary["spend_usd"] == 0
    assert summary["drafted"] == []
    assert "NOTHING WAS POSTED" in Path(summary["report"]).read_text(encoding="utf-8")
    state = growth.load_state(tmp_path / "state.json")
    assert state["community_threads"] == {}


# -- daily plan feed --------------------------------------------------------------

def _plan_state(**over):
    state = {"last_run": {}, "last_attempt": {}, "metrics": [],
             "experiments": {}, "strategy_history": [], "spend_usd": 0.0}
    state.update(over)
    return state


def test_daily_plan_lists_drafted_replies_before_due_jobs():
    state = _plan_state(community_threads={
        "a1": {"id": "a1", "venue": "r/cissp", "title": "How do I study?",
               "url": "https://www.reddit.com/r/cissp/comments/a1/x/",
               "intent_score": 85, "intent": "high",
               "reasons": ["asks a question"], "status": "drafted",
               "draft_path": "reports/community_drafts/a1.md",
               "created_utc": NOW.timestamp()},
        "b2": {"id": "b2", "venue": "r/cissp", "title": "Best course?",
               "url": "https://x/b2", "intent_score": 60,
               "intent": "worth_reply", "reasons": [],
               "status": "found", "created_utc": NOW.timestamp()},
    })
    plan = daily_plan.build_daily_plan(
        business_name="Road to CISSP", site_url=SITE, state=state, now=NOW,
        cadence=CADENCE, retry_after=RETRY_AFTER)
    ids = [i["id"] for i in plan["items"]]
    assert "reply:a1" in ids
    assert "reply:b2" not in ids  # found, not drafted — not the owner's move
    reply = next(i for i in plan["items"] if i["id"] == "reply:a1")
    assert reply["channel"] == "community"
    assert reply["source_key"] == "thread:a1:drafted"
    # Replies outrank due jobs (audit is due on a fresh state).
    assert ids.index("reply:a1") < ids.index("job:audit")


def test_cli_daily_plan_done_flips_thread_to_done(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    state = _plan_state(community_threads={
        "a1": {"id": "a1", "venue": "r/cissp", "title": "How do I study?",
               "url": "https://www.reddit.com/r/cissp/comments/a1/x/",
               "intent_score": 85, "intent": "high",
               "reasons": ["asks a question"], "status": "drafted",
               "draft_path": "reports/community_drafts/a1.md",
               "created_utc": NOW.timestamp()},
    })
    growth.save_state(tmp_path / "state.json", state)
    # daily-plan-done only marks items on a recorded plan for that date.
    assert growth.main(["daily-plan", "--date", "2026-11-07"]) == 0
    capsys.readouterr()
    code = growth.main(["daily-plan-done", "reply:a1",
                        "--date", "2026-11-07"])
    assert code == 0
    assert "reply:a1: done" in capsys.readouterr().out
    assert growth.load_state(tmp_path / "state.json")[
        "community_threads"]["a1"]["status"] == "done"
    # And the reply is gone from the next day's plan.
    plan = daily_plan.build_daily_plan(
        business_name="Road to CISSP", site_url=SITE,
        state=growth.load_state(tmp_path / "state.json"),
        now=NOW + timedelta(days=1), cadence=CADENCE,
        retry_after=RETRY_AFTER)
    assert "reply:a1" not in [i["id"] for i in plan["items"]]

"""Daily plan: ranking, carry-over, done/skip, cap, CLI and dashboard.

No network, no LLM, no spend, no publishing anywhere in this file —
the builder is pure, and the CLI tests run it against tmp state only.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ecosystem import daily_plan
from ecosystem.road2cissp_growth import (
    CADENCE, RETRY_AFTER, load_state, save_state,
)
import ecosystem.road2cissp_growth as growth

T0 = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def base_state(**over):
    state = {
        "last_run": {},
        "last_attempt": {},
        "metrics": [],
        "experiments": {},
        "strategy_history": [],
        "spend_usd": 0.0,
    }
    state.update(over)
    return state


def build(state, now=T0, **kw):
    return daily_plan.build_daily_plan(
        business_name="Road to CISSP", site_url="https://road2cissp.com",
        state=state, now=now, cadence=CADENCE, retry_after=RETRY_AFTER, **kw)


def ids(plan):
    return [item["id"] for item in plan["items"]]


# -- empty state: the first-day plan ---------------------------------------

def test_empty_state_is_audit_keywords_and_measurement():
    plan = build(base_state())
    assert plan["date"] == "2026-11-07"
    # All jobs are due on day one (nothing has ever run); metrics has
    # no entries yet, so job:metrics is replaced by the measurement gap.
    assert ids(plan) == ["job:audit", "job:keywords", "job:strategy",
                         "metrics:record-first"]
    assert plan["items"][2]["needs_llm"] is True  # strategy needs a key
    assert plan["truncated"] == 0
    # No strategy yet, so no SEO article and no this-week tasks.
    assert not any(i["id"].startswith("seo:") for i in plan["items"])
    text = daily_plan.render_text(plan)
    assert "Daily plan — Road to CISSP (2026-11-07)" in text
    md = daily_plan.render_markdown(plan)
    assert "NOTHING WAS PUBLISHED" in md
    assert "NOTHING WAS PUBLISHED" in daily_plan.BANNER


def test_builder_is_deterministic_and_pure():
    state = base_state()
    assert build(state) == build(state)
    before = json.dumps(state, sort_keys=True, default=str)
    build(state)
    assert json.dumps(state, sort_keys=True, default=str) == before


# -- ranking ----------------------------------------------------------------

def _ranked_state():
    return base_state(
        last_run={job: (T0 - timedelta(hours=1)).isoformat()
                  for job in ("audit", "keywords", "metrics", "strategy")},
        metrics=[{"at": T0.isoformat(), "visitors": 10}],
        keywords={"at": T0.isoformat(), "items": [
            {"keyword": "cissp study guide", "avg_12mo": 40.0,
             "trend": "rising"}]},
        audit={"at": T0.isoformat(), "summary": "x", "pages_checked": 3,
               "severity_counts": {"critical": 1},
               "findings": [{"severity": "critical", "area": "analytics",
                             "issue": "No analytics",
                             "fix": "Install Plausible"}]},
        strategy={
            "summary": "Fix crawlability first.",
            "north_star": {"metric": "weekly sign-ins", "current": None,
                           "target_90d": 150},
            "site_fixes": [{"fix": "Prerender routes",
                            "why": "Google sees one page", "priority": "P0"}],
            "this_week": ["Install Plausible", "Verify Search Console"],
        },
        experiments={
            "reddit-answers": {"slug": "reddit-answers", "name": "Reddit answers",
                               "channel": "community", "status": "proposed",
                               "hypothesis": "helpful answers drive visits",
                               "kpi": "referral visits", "target": "100 visits",
                               "duration_days": 14, "budget_usd": 0},
            "domain-explainers": {"slug": "domain-explainers",
                                  "name": "Domain explainers", "channel": "SEO",
                                  "status": "review", "duration_days": 28,
                                  "budget_usd": 0},
        },
    )


def test_ranking_fix_then_decision_then_tasks_then_seo():
    plan = build(_ranked_state())
    assert ids(plan) == [
        "fix:prerender-routes",
        "fix:no-analytics",
        "approve:reddit-answers",
        "outcome:domain-explainers",
        "task:1",
        "task:2",
        "seo:next-article",
    ]
    assert [i["rank"] for i in plan["items"]] == list(range(1, 8))
    assert plan["north_star"]["metric"] == "weekly sign-ins"
    assert plan["counts"]["needs_owner"] == 6


def test_due_jobs_appear_in_run_order_and_only_when_due():
    state = base_state(metrics=[{"at": T0.isoformat(), "visitors": 5}])
    plan = build(state)
    assert [i for i in ids(plan) if i.startswith("job:")] == [
        "job:audit", "job:keywords", "job:metrics", "job:strategy"]
    # Building a plan runs nothing, so the jobs stay due an hour later
    # — the list is a to-do list, not a record of work done.
    assert ids(build(state, now=T0 + timedelta(hours=1))) == ids(plan)
    # Once the jobs have actually run (last_run set), nothing is due
    # until the cadence elapses again.
    ran = base_state(
        metrics=[{"at": T0.isoformat(), "visitors": 5}],
        last_run={job: T0.isoformat()
                  for job in ("audit", "keywords", "metrics", "strategy")},
    )
    plan2 = build(ran, now=T0 + timedelta(hours=1))
    assert plan2["items"] == []
    assert "Nothing needs you right now." in daily_plan.render_text(plan2)
    plan3 = build(ran, now=T0 + timedelta(days=8))
    assert [i for i in ids(plan3) if i.startswith("job:")] == [
        "job:audit", "job:keywords", "job:metrics", "job:strategy"]


def test_failed_job_backs_off_instead_of_reappearing():
    state = base_state(
        metrics=[{"at": T0.isoformat(), "visitors": 5}],
        last_attempt={"strategy": T0.isoformat()},
        last_run={},
    )
    # Strategy failed at T0: not due again within 6h, other jobs are due.
    plan = build(state, now=T0 + timedelta(hours=1))
    assert "job:strategy" not in ids(plan)
    assert "job:audit" in ids(plan)
    plan2 = build(state, now=T0 + timedelta(hours=7))
    assert "job:strategy" in ids(plan2)


def test_active_experiment_missing_drafts_is_a_draft_item():
    state = base_state(
        last_run={job: (T0 - timedelta(hours=1)).isoformat()
                  for job in ("audit", "keywords", "metrics", "strategy")},
        metrics=[{"at": T0.isoformat(), "visitors": 5}],
        experiments={
            "reddit-answers": {"slug": "reddit-answers", "name": "Reddit answers",
                               "channel": "community", "status": "active",
                               "content_needed": ["community"], "drafts": None},
            "done-one": {"slug": "done-one", "name": "Done", "channel": "SEO",
                         "status": "active", "content_needed": ["blog"],
                         "drafts": ["reports/x.md"]},
        },
    )
    plan = build(state)
    assert ids(plan) == ["draft:reddit-answers"]
    assert plan["items"][0]["needs_llm"] is True


# -- carry-over, done, skip, cap -------------------------------------------

def test_carry_over_then_done_disappears():
    state = base_state(
        strategy={"this_week": ["Install Plausible", "Verify Search Console"]},
        last_run={job: (T0 - timedelta(hours=1)).isoformat()
                  for job in ("audit", "keywords", "metrics", "strategy")},
        metrics=[{"at": T0.isoformat(), "visitors": 5}],
    )
    day1 = build(state, now=T0)
    assert ids(day1) == ["task:1", "task:2"]
    daily_plan.record_plan(state, day1)

    # Day 2: the daily metrics job is due again (it never ran), and the
    # two tasks carry over ahead of nothing else new.
    day2 = build(state, now=T0 + timedelta(days=1))
    assert ids(day2) == ["job:metrics", "task:1", "task:2"]
    assert [i["carried_over"] for i in day2["items"]] == [False, True, True]
    daily_plan.record_plan(state, day2)
    daily_plan.mark_item(state, "2026-11-08", "task:1", "done",
                         source_key="strategy:")

    # Day 3: the done task stays hidden (its source — this strategy —
    # has not changed), the other task still carries over, and the
    # metrics job is due again.
    day3 = build(state, now=T0 + timedelta(days=2))
    assert ids(day3) == ["job:metrics", "task:2"]
    assert day3["items"][1]["carried_over"] is True


def test_done_expires_when_the_source_changes():
    # A done task stays hidden while the strategy is the same, but a
    # new strategy cycle is new work — the same slot reappears.
    state = base_state(
        strategy={"cycle": 1,
                  "this_week": ["Install Plausible"]},
        last_run={job: (T0 - timedelta(hours=1)).isoformat()
                  for job in ("audit", "keywords", "metrics", "strategy")},
        metrics=[{"at": T0.isoformat(), "visitors": 5}],
    )
    day1 = build(state, now=T0)
    daily_plan.record_plan(state, day1)
    daily_plan.mark_item(state, "2026-11-07", "task:1", "done",
                         source_key="strategy:1")
    assert ids(build(state, now=T0 + timedelta(days=1))) == ["job:metrics"]
    state["strategy"] = {"cycle": 2, "this_week": ["Verify Search Console"]}
    assert "task:1" in ids(build(state, now=T0 + timedelta(days=1)))


def test_mark_item_rejects_bad_status_and_unknown_item():
    state = base_state()
    plan = build(state)
    daily_plan.record_plan(state, plan)
    with pytest.raises(ValueError, match="status must be"):
        daily_plan.mark_item(state, "2026-11-07", "job:audit", "maybe")
    with pytest.raises(ValueError, match="not on the 2026-11-07 daily plan"):
        daily_plan.mark_item(state, "2026-11-07", "job:nope", "done")
    with pytest.raises(ValueError, match="not on the 2026-11-08 daily plan"):
        daily_plan.mark_item(state, "2026-11-08", "job:audit", "done")


def test_cap_at_ten_and_truncated_count():
    state = base_state(
        strategy={"this_week": [f"Task {n}" for n in range(1, 16)]},
        last_run={job: (T0 - timedelta(hours=1)).isoformat()
                  for job in ("audit", "keywords", "metrics", "strategy")},
        metrics=[{"at": T0.isoformat(), "visitors": 5}],
    )
    plan = build(state)
    assert len(plan["items"]) == 10
    assert plan["truncated"] == 5
    assert "…and 5 more" in daily_plan.render_text(plan)


# -- CLI ---------------------------------------------------------------------

def test_cli_daily_plan_writes_report_and_done_marks_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    state = _ranked_state()
    save_state(tmp_path / "state.json", state)

    assert growth.main(["daily-plan", "--date", "2026-11-07"]) == 0
    out = capsys.readouterr().out
    assert "Daily plan — Road to CISSP (2026-11-07)" in out
    assert "1. [site] Fix (P0): Prerender routes" in out
    assert "report: " in out
    report = tmp_path / "reports" / "daily_plan_20261107.md"
    assert report.exists()
    assert "NOTHING WAS PUBLISHED" in report.read_text(encoding="utf-8")

    saved = load_state(tmp_path / "state.json")
    assert saved["daily_plans"]["2026-11-07"]["items"][0] == "fix:prerender-routes"

    assert growth.main(["daily-plan-done", "fix:prerender-routes",
                        "--date", "2026-11-07"]) == 0
    saved = load_state(tmp_path / "state.json")
    assert saved["daily_plans"]["2026-11-07"]["status"][
        "fix:prerender-routes"] == "done"

    # Tomorrow's plan no longer lists the done fix.
    assert growth.main(["daily-plan", "--date", "2026-11-08"]) == 0
    out = capsys.readouterr().out
    assert "Fix (P0): Prerender routes" not in out
    assert "[carried over]" in out  # the rest carried over


def test_cli_done_unknown_item_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    save_state(tmp_path / "state.json", base_state())
    assert growth.main(["daily-plan-done", "job:audit",
                        "--date", "2026-11-07"]) == 1
    assert "not on the 2026-11-07 daily plan" in capsys.readouterr().out


# -- dashboard ---------------------------------------------------------------

def test_dashboard_daily_plan_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from dashboard.app import create_app

    state = _ranked_state()
    path = tmp_path / "state.json"
    save_state(path, state)
    monkeypatch.setenv("DAILY_PLAN_STATE_PATH", str(path))
    client = TestClient(create_app())
    r = client.get("/api/daily-plan")
    assert r.status_code == 200
    plan = r.json()
    assert plan["business"] == "Road to CISSP"
    assert plan["items"][0]["id"] == "fix:prerender-routes"
    assert any(i["id"] == "approve:reddit-answers" for i in plan["items"])

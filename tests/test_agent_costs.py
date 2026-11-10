"""Agent costs are visible: Overview card + honest job costs."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _signin import OPERATOR_EMAIL, operator_store_and_config, sign_in
from core.audit import AuditLog
from ventoryn.app import agent_cost_windows, create_app
from ventoryn.saas import job_cost_label

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _event(log, event, run_id, cost, tokens, age_days):
    log.record("research", event, run_id=run_id, task_id=f"t-{run_id}",
               cost_usd=cost, tokens_used=tokens,
               result={"passed": True} if event == "task_verified" else {})
    log._events[-1].created_at = NOW - timedelta(days=age_days)


def test_cost_windows_count_each_run_once():
    log = AuditLog()
    # One run logged twice (worker + orchestrator) must cost once.
    _event(log, "run_completed", "r1", 0.15, 4000, 0.1)
    _event(log, "task_verified", "r1", 0.15, 4000, 0.1)
    _event(log, "run_completed", "r2", 0.05, 1000, 3)
    _event(log, "run_completed", "r3", 0.50, 9000, 20)
    _event(log, "run_completed", "r4", 9.99, 1, 40)  # outside 30 days
    log.record("worker", "tick_completed", cost_usd=5.0)  # not a run event
    w = agent_cost_windows(log, NOW)
    assert round(w[1]["cost"], 4) == 0.15 and w[1]["runs"] == 1 and w[1]["tokens"] == 4000
    assert round(w[7]["cost"], 4) == 0.20 and w[7]["runs"] == 2
    assert round(w[30]["cost"], 4) == 0.70 and w[30]["runs"] == 3


def test_job_cost_label():
    assert job_cost_label({"job_type": "seo_article", "credits_charged": 10}, False) == "10 credits"
    assert job_cost_label({"job_type": "seo_article", "credits_charged": 0}, True) == \
        "10 credits — not charged (owner)"
    assert job_cost_label({"job_type": "geo_audit", "credits_charged": 0}, False) == "0 credits"


def _app():
    store, config = operator_store_and_config()
    audit = AuditLog()
    app = create_app(audit=audit, saas_store=store, saas_config=config)
    return app, store, audit


def test_overview_shows_real_ai_spend_to_the_owner():
    app, store, audit = _app()
    audit.record("research", "run_completed", run_id="r1", task_id="t1",
                 cost_usd=0.0123, tokens_used=2500)
    client = TestClient(app)
    owner = sign_in(client, store)
    site = store.create_site(owner["id"], "https://shop.example.com")
    store.charge_credits(owner["id"], "seo_article", site["id"], is_operator=True)
    html = client.get("/ops").text
    assert "<h2>Agent costs</h2>" in html
    assert "$0.0123" in html and "2,500 tokens" in html
    assert "10 credits (not charged)" in html
    assert "href='/agents'>Cost per agent</a>" in html
    jobs = client.get("/portfolio").text
    assert "10 credits — not charged (owner)" in jobs


def test_customer_sees_credits_not_platform_spend():
    app, store, audit = _app()
    audit.record("research", "run_completed", run_id="r1", task_id="t1",
                 cost_usd=0.0123, tokens_used=2500)
    client = TestClient(app)
    cust = sign_in(client, store, email="ada@example.com", sub="sub-ada", name="Ada")
    store.grant_credits(cust["id"], 50, "test")
    site = store.create_site(cust["id"], "https://ada.example.com")
    store.charge_credits(cust["id"], "geo_audit", site["id"])
    html = client.get("/ops").text
    assert "<h2>Agent costs</h2>" in html
    assert "5 credits" in html
    assert "$0.0123" not in html  # the platform's AI spend is the owner's
    assert OPERATOR_EMAIL not in html

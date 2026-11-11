"""Autopilot: the agents work on every website by default."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from test_saas_shell import _account, _login, client, config, store  # noqa: F401
from ventoryn import autopilot, site_jobs

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def fake_jobs(monkeypatch):
    calls = []

    def fake_run(job_type, site, inputs, *, orchestrator, reports_dir, now=None, store=None):
        calls.append((job_type, site["id"]))
        if site["host"].startswith("broken"):
            raise RuntimeError("site unreachable")
        return f"{job_type} ok · report x.md", "x.md"

    monkeypatch.setattr(site_jobs, "run", fake_run)
    monkeypatch.setattr(site_jobs, "server_blocker", lambda jt: None)
    return calls


def _run(store, config, **kw):
    return autopilot.run_once(store, config, orchestrator=None,
                              reports_dir=Path("."), now=kw.pop("now", NOW), **kw)


def test_every_website_gets_its_jobs_then_waits_a_week(store, config, fake_jobs):
    store.grant_credits(_account(store)["id"], 100, "test")
    a = store.create_site(_account(store)["id"], "https://shop.example.com")
    b = store.create_site(_account(store, sub="s2", email="bo@example.com")["id"],
                          "https://blog.example.org")
    store.grant_credits(b["account_id"], 100, "test")
    ran = _run(store, config, max_jobs=10)
    assert {(j["site_id"], j["job_type"]) for j in ran} == {
        (a["id"], "analyze"), (a["id"], "seo_article"), (a["id"], "x_campaign"),
        (b["id"], "analyze"), (b["id"], "seo_article"), (b["id"], "x_campaign")}
    assert all(j["status"] == "done" and j["result"].startswith("Automatic") for j in ran)
    assert _run(store, config, max_jobs=10, now=NOW + timedelta(days=6)) == []
    assert len(_run(store, config, max_jobs=10, now=NOW + timedelta(days=7, seconds=1))) == 6


def test_customers_pay_normal_credits_and_are_skipped_when_short(store, config):
    acct = _account(store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    store.grant_credits(acct["id"], 7, "test")  # analyze 5 fits, seo 10 does not
    ran = _run(store, config, max_jobs=10)
    assert [j["job_type"] for j in ran] == ["analyze"]
    assert store.credit_balance(acct["id"]) == 2
    assert store.last_job(site["id"], "seo_article") is None


def test_failed_job_is_refunded_and_retried_after_a_day(store, config):
    acct = _account(store)
    store.grant_credits(acct["id"], 100, "test")
    site = store.create_site(acct["id"], "https://broken.example.com")
    ran = _run(store, config, max_jobs=1)
    assert ran[0]["status"] == "failed" and "refunded 5" in ran[0]["result"]
    assert store.credit_balance(acct["id"]) == 100
    assert "analyze" not in autopilot.due_jobs(store, store.get_site(site["id"]),
                                               NOW + timedelta(hours=23))
    assert "analyze" in autopilot.due_jobs(store, store.get_site(site["id"]),
                                           NOW + timedelta(days=1, minutes=1))


def test_owner_can_turn_it_off_and_on(client, store, config):
    acct = _login(client, store)
    store.grant_credits(acct["id"], 100, "test")
    site = store.create_site(acct["id"], "https://shop.example.com")
    page = client.get(f"/account/sites/{site['id']}").text
    assert "Agents on autopilot" in page
    assert "class='v-switch' role='switch' aria-checked='true'" in page
    r = client.post(f"/account/sites/{site['id']}/autopilot", data={"on": "0"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _run(store, config, max_jobs=10) == []
    client.post(f"/account/sites/{site['id']}/autopilot", data={"on": "1"})
    assert len(_run(store, config, max_jobs=10)) == 3


def test_cannot_toggle_someone_elses_site(client, store):
    _login(client, store)
    other = _account(store, sub="s2", email="bo@example.com")
    site = store.create_site(other["id"], "https://blog.example.org")
    r = client.post(f"/account/sites/{site['id']}/autopilot", data={"on": "0"})
    assert r.status_code == 404
    assert store.get_site(site["id"])["autopilot"] == 1


def test_off_switch_for_the_loop(monkeypatch):
    monkeypatch.setenv("VENTORYN_AUTOPILOT", "off")
    assert autopilot.start(None, None, get_orchestrator=lambda: None,
                           reports_dir=Path(".")) is None



def test_switch_flips_in_place_and_list_redirects_back(client, store, config):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    listing = client.get("/portfolio").text
    assert f"action='/account/sites/{site['id']}/autopilot'" in listing
    assert "name='return_to' value='/portfolio'" in listing
    r = client.post(f"/account/sites/{site['id']}/autopilot", data={"on": "0"},
                    headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json() == {"on": False}
    assert store.get_site(site["id"])["autopilot"] == 0
    r = client.post(f"/account/sites/{site['id']}/autopilot",
                    data={"on": "1", "return_to": "/portfolio"}, follow_redirects=False)
    assert r.headers["location"] == "/portfolio"
    r = client.post(f"/account/sites/{site['id']}/autopilot",
                    data={"on": "1", "return_to": "https://evil.example"}, follow_redirects=False)
    assert r.headers["location"] == f"/account/sites/{site['id']}#autopilot"


def test_job_orphaned_by_a_restart_is_refunded_and_retried(store, config):
    acct = _account(store)
    store.grant_credits(acct["id"], 100, "test")
    site = store.create_site(acct["id"], "https://shop.example.com")
    stuck = store.charge_credits(acct["id"], "seo_article", site["id"])  # stays queued
    assert "seo_article" not in autopilot.due_jobs(store, store.get_site(site["id"]), NOW)
    later = NOW + timedelta(hours=3)
    assert autopilot.expire_stale_jobs(store, later) == 1
    job = store.get_job(stuck["id"])
    assert job["status"] == "failed" and "refunded 10" in job["result"]
    assert store.credit_balance(acct["id"]) == 100
    assert "seo_article" in autopilot.due_jobs(store, store.get_site(site["id"]),
                                               later + timedelta(days=1, minutes=1))

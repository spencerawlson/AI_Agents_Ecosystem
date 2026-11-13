"""Plan gating: every tier limit is enforced server-side, not just in
the UI — a direct request is refused the same as a blocked button.
Legacy accounts (plan == "") are never limited; the operator
bypasses every check."""

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_saas_shell import _account, _login, client, config, store  # noqa: F401
from ventoryn import autopilot, saas, site_jobs
from ventoryn.app import create_app
from ventoryn.entitlements import (
    can_activate_agents,
    can_add_team_seat,
    can_connect_integration,
    can_create_campaign,
    can_create_workspace,
    can_use_api,
    can_view_advanced_analytics,
    count_active_agents,
    count_active_campaigns,
    is_autopilot_site_within_cap,
)
from ventoryn.saas import PlanLimitExceeded

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def fake_jobs(monkeypatch):
    """Stub the agent runs (same pattern as test_autopilot.py)."""
    calls = []

    def fake_run(job_type, site, inputs, *, orchestrator, reports_dir,
                 now=None, store=None):
        calls.append((job_type, site["id"]))
        return f"{job_type} ok · report x.md", "x.md"

    monkeypatch.setattr(site_jobs, "run", fake_run)
    monkeypatch.setattr(site_jobs, "server_blocker", lambda jt: None)
    monkeypatch.setattr(saas, "_now", lambda: NOW)
    return calls


def _tier_account(store, plan, sub, email):
    account = _account(store, sub=sub, email=email)
    store.set_plan(account["id"], plan)
    return store.get_account(account["id"])


def _site(store, account_id, url):
    return store.create_site(account_id, url)


def _campaign(store, site, account_id, *, status=None, drafts="pending"):
    """A campaign on ``site``; ``drafts`` is the status its one draft
    ends in ('pending', 'scheduled', 'posted', 'rejected')."""
    c = store.create_campaign(site["id"], account_id, days=30,
                              posts_per_day=1, with_link=True,
                              source="manual")
    d = store.create_social_draft(site["id"], "x", "Hello", "https://x.example",
                                  campaign_id=c["id"])
    if drafts != "pending":
        store._conn.execute(
            "UPDATE saas_social_drafts SET status = ? WHERE id = ?",
            (drafts, d["id"]))
        store._conn.commit()
    if status is not None:
        store.set_campaign_status(c["id"], status)
    return store.get_campaign(c["id"])


def _run(store, config, **kw):
    return autopilot.run_once(store, config, orchestrator=None,
                              reports_dir=Path("."), now=kw.pop("now", NOW),
                              **kw)


# -- workspaces: enforced in the store itself ------------------------------

def test_workspace_cap_refused_in_store_for_starter(store):
    account = _tier_account(store, "starter", "ws-1", "ws1@example.com")
    store.ensure_default_workspace(account["id"])
    assert len(store.list_workspaces(account["id"])) == 1
    with pytest.raises(PlanLimitExceeded):
        store.create_workspace(account["id"], "Second")


def test_workspace_cap_refused_in_store_for_growth(store):
    account = _tier_account(store, "growth", "ws-2", "ws2@example.com")
    store.ensure_default_workspace(account["id"])
    store.create_workspace(account["id"], "Second")
    store.create_workspace(account["id"], "Third")
    with pytest.raises(PlanLimitExceeded):
        store.create_workspace(account["id"], "Fourth")


def test_workspace_cap_allows_ten_for_pro(store):
    account = _tier_account(store, "pro", "ws-3", "ws3@example.com")
    store.ensure_default_workspace(account["id"])
    for i in range(9):
        store.create_workspace(account["id"], f"W{i}")
    assert len(store.list_workspaces(account["id"])) == 10
    with pytest.raises(PlanLimitExceeded):
        store.create_workspace(account["id"], "Eleventh")


def test_workspace_never_limited_for_legacy(store):
    account = _account(store)
    assert account["plan"] == ""
    for i in range(5):
        store.create_workspace(account["id"], f"W{i}")
    assert len(store.list_workspaces(account["id"])) == 5


def test_workspace_default_still_created_for_tier_account(store):
    # ensure_default_workspace only creates when the account has
    # none — a fresh tier account is always under its cap.
    account = _tier_account(store, "starter", "ws-4", "ws4@example.com")
    assert store.ensure_default_workspace(account["id"])["name"] == "Default"


# -- active agents -----------------------------------------------------------

def test_active_agent_cap_for_each_tier(store):
    cases = (("starter", 2), ("growth", 5), ("pro", 15))
    for n, (plan, cap) in enumerate(cases):
        account = _tier_account(store, plan, f"ag-{n}", f"ag{n}@example.com")
        for i in range(cap):
            _site(store, account["id"], f"https://s{i}.example.com")
        assert count_active_agents(store, account["id"]) == cap
        ok, message = can_activate_agents(store, account)
        assert not ok and f"allows {cap} active agent" in message
        # Turning one off frees the slot again.
        sites = store.list_sites(account["id"])
        store.set_site_autopilot(sites[-1]["id"], False)
        ok, _ = can_activate_agents(store, account)
        assert ok


def test_active_agents_never_limited_for_legacy_and_operator(store):
    account = _account(store)
    for i in range(5):
        _site(store, account["id"], f"https://l{i}.example.com")
    ok, _ = can_activate_agents(store, account)
    assert ok
    ok, _ = can_activate_agents(store, account, is_operator=True)
    assert ok
    tier = _tier_account(store, "starter", "ag-op", "agop@example.com")
    for i in range(3):
        _site(store, tier["id"], f"https://o{i}.example.com")
    ok, _ = can_activate_agents(store, tier)
    assert not ok
    ok, _ = can_activate_agents(store, tier, is_operator=True)
    assert ok


def test_autopilot_slot_rank_is_oldest_first(store):
    account = _tier_account(store, "starter", "ag-r", "agr@example.com")
    sites = [_site(store, account["id"], f"https://r{i}.example.com")
             for i in range(3)]
    # Starter allows 2: the two oldest sites keep their slots.
    assert is_autopilot_site_within_cap(store, account, sites[0])
    assert is_autopilot_site_within_cap(store, account, sites[1])
    assert not is_autopilot_site_within_cap(store, account, sites[2])
    # Turning the newest off does not promote anything — the rank
    # is by creation order, not by which sites are on.
    store.set_site_autopilot(sites[2]["id"], False)
    assert not is_autopilot_site_within_cap(store, account, sites[2])


def test_autopilot_toggle_refused_over_cap(client, store, config):
    account = _tier_account(store, "starter", "ag-t", "agt@example.com")
    _login(client, store, account)
    sites = [_site(store, account["id"], f"https://t{i}.example.com")
             for i in range(3)]
    store.set_site_autopilot(sites[2]["id"], False)
    # Turning the third back on is refused (2 already on, cap 2).
    r = client.post(f"/websites/sites/{sites[2]['id']}/autopilot",
                    data={"on": "1"}, headers={"accept": "application/json"})
    assert r.status_code == 403
    assert "allows 2 active agents" in r.json()["error"]
    assert store.get_site(sites[2]["id"])["autopilot"] == 0
    # Turning one off is always allowed.
    r = client.post(f"/websites/sites/{sites[0]['id']}/autopilot",
                    data={"on": "0"})
    assert r.status_code in (200, 303)
    assert store.get_site(sites[0]["id"])["autopilot"] == 0


def test_autopilot_loop_skips_sites_beyond_cap(store, config, fake_jobs):
    account = _tier_account(store, "starter", "ag-l", "agl@example.com")
    store.grant_wallet_credit(account["id"], 1000000, "test")
    sites = [_site(store, account["id"], f"https://lp{i}.example.com")
             for i in range(3)]
    ran = _run(store, config, max_jobs=10)
    assert len(ran) == 6  # analyze + seo + x_campaign on the first two
    assert {s for _, s in fake_jobs} == {sites[0]["id"], sites[1]["id"]}


# -- active campaigns --------------------------------------------------------

def test_active_campaign_counting(store):
    account = _tier_account(store, "starter", "camp-1", "c1@example.com")
    site = _site(store, account["id"], "https://c.example.com")
    assert count_active_campaigns(store, account["id"]) == 0
    _campaign(store, site, account["id"])  # proposed, pending draft
    assert count_active_campaigns(store, account["id"]) == 1
    _campaign(store, site, account["id"], status="scheduled")  # approved
    assert count_active_campaigns(store, account["id"]) == 2
    _campaign(store, site, account["id"], status="rejected")
    assert count_active_campaigns(store, account["id"]) == 2
    # A scheduled campaign whose drafts all posted is finished.
    _campaign(store, site, account["id"], status="scheduled", drafts="posted")
    assert count_active_campaigns(store, account["id"]) == 2
    ok, message = can_create_campaign(store, account)
    assert not ok and "allows 2 active campaigns" in message


def test_campaign_cap_for_each_tier(store):
    for n, (plan, cap) in enumerate(
            (("starter", 2), ("growth", 10), ("pro", 50))):
        account = _tier_account(store, plan, f"cc-{n}", f"cc{n}@example.com")
        site = _site(store, account["id"], f"https://cc{n}.example.com")
        for _ in range(cap):
            _campaign(store, site, account["id"])
        assert count_active_campaigns(store, account["id"]) == cap
        ok, _ = can_create_campaign(store, account)
        assert not ok


def test_campaign_never_limited_for_legacy_and_operator(store):
    account = _account(store)
    site = _site(store, account["id"], "https://cl.example.com")
    for _ in range(3):
        _campaign(store, site, account["id"])
    ok, _ = can_create_campaign(store, account)
    assert ok
    tier = _tier_account(store, "starter", "camp-op", "campop@example.com")
    site2 = _site(store, tier["id"], "https://co.example.com")
    for _ in range(2):
        _campaign(store, site2, tier["id"])
    ok, _ = can_create_campaign(store, tier)
    assert not ok
    ok, _ = can_create_campaign(store, tier, is_operator=True)
    assert ok


def test_campaign_start_route_refused_over_cap(client, store, config,
                                               monkeypatch):
    monkeypatch.setattr(site_jobs, "server_blocker", lambda jt: None)
    account = _tier_account(store, "starter", "camp-r", "campr@example.com")
    _login(client, store, account)
    site = _site(store, account["id"], "https://cr.example.com")
    for _ in range(2):
        _campaign(store, site, account["id"])
    r = client.post("/agents/campaigns",
                    data={"site_id": site["id"], "days": "30",
                          "posts_per_day": "1", "with_link": "1"})
    assert r.status_code == 403
    assert "allows 2 active campaigns" in r.text


def test_autopilot_skips_x_campaign_over_cap(store, config, fake_jobs):
    account = _tier_account(store, "starter", "camp-l", "campl@example.com")
    store.grant_wallet_credit(account["id"], 1000000, "test")
    site1 = _site(store, account["id"], "https://x1.example.com")
    site2 = _site(store, account["id"], "https://x2.example.com")
    # Two open proposals on site2 fill the Starter cap; x_campaign
    # is due for site1 (no open proposal there) but must be skipped.
    for _ in range(2):
        _campaign(store, site2, account["id"])
    ran = _run(store, config, max_jobs=10)
    assert ("x_campaign", site1["id"]) not in fake_jobs
    assert ("analyze", site1["id"]) in fake_jobs
    assert ("seo_article", site1["id"]) in fake_jobs


# -- team seats ---------------------------------------------------------------

def test_team_seats_allowed_until_invites_exist(store):
    account = _tier_account(store, "starter", "seat-1", "seat1@example.com")
    ok, _ = can_add_team_seat(store, account)
    assert ok  # no invite flow yet, so the count is 0
    legacy = _account(store)
    ok, _ = can_add_team_seat(store, legacy)
    assert ok


# -- advanced analytics ---------------------------------------------------------

def test_advanced_analytics_per_plan(store):
    cases = (("starter", False), ("growth", True), ("pro", True))
    for n, (plan, allowed) in enumerate(cases):
        account = _tier_account(store, plan, f"an-{n}", f"an{n}@example.com")
        ok, message = can_view_advanced_analytics(store, account)
        assert ok is allowed
        if not allowed:
            assert "Growth or Pro" in message
    legacy = _account(store)
    ok, _ = can_view_advanced_analytics(store, legacy)
    assert ok
    tier = _tier_account(store, "starter", "an-op", "anop@example.com")
    ok, _ = can_view_advanced_analytics(store, tier, is_operator=True)
    assert ok


def test_analytics_page_hides_advanced_for_starter(client, store, config):
    account = _tier_account(store, "starter", "an-p", "anp@example.com")
    _login(client, store, account)
    html = client.get("/analytics").text
    assert "Funnel" in html  # basic analytics stays
    assert "Advanced analytics" in html
    assert "Acquisition trend" not in html
    assert "href='/report'" not in html


def test_analytics_page_shows_advanced_for_growth(client, store, config):
    account = _tier_account(store, "growth", "an-g", "ang@example.com")
    _login(client, store, account)
    html = client.get("/analytics").text
    assert "Acquisition trend" in html
    assert "href='/report'" in html


def test_report_page_gated_for_starter(client, store, config):
    account = _tier_account(store, "starter", "rep-1", "rep1@example.com")
    _login(client, store, account)
    r = client.get("/report")
    assert r.status_code == 403
    assert "Advanced analytics" in r.text
    store.set_plan(account["id"], "growth")
    r = client.get("/report")
    assert r.status_code == 200


# -- premium integrations -------------------------------------------------------

def test_integration_matrix(store):
    matrix = (
        # (plan, kind, allowed)
        ("starter", "x", True),  # Starter posts to X (Spencer, 2026-11-13)
        ("starter", "facebook", False),
        ("starter", "instagram", False),
        ("starter", "tiktok", False),
        ("starter", "plausible", True),
        ("starter", "stripe_sales", True),
        ("growth", "x", True),
        ("growth", "facebook", True),
        ("growth", "instagram", True),
        ("growth", "tiktok", False),  # Pro-only while TikTok is in review
        ("growth", "plausible", True),
        ("pro", "x", True),
        ("pro", "facebook", True),
        ("pro", "instagram", True),
        ("pro", "tiktok", True),
        ("pro", "plausible", True),
    )
    for n, (plan, kind, allowed) in enumerate(matrix):
        account = _tier_account(store, plan, f"in-{n}", f"in{n}@example.com")
        ok, message = can_connect_integration(store, account, kind)
        assert ok is allowed, (plan, kind, ok)
        if not allowed:
            assert "Upgrade" in message


def test_integration_never_limited_for_legacy_and_operator(store):
    legacy = _account(store)
    for kind in ("x", "facebook", "instagram", "tiktok"):
        ok, _ = can_connect_integration(store, legacy, kind)
        assert ok
    tier = _tier_account(store, "starter", "in-op", "inop@example.com")
    ok, _ = can_connect_integration(store, tier, "facebook")
    assert not ok
    ok, _ = can_connect_integration(store, tier, "facebook", is_operator=True)
    assert ok


def test_reddit_refused_for_every_plan(store):
    for plan in ("starter", "growth", "pro"):
        account = _tier_account(store, plan, f"rd-{plan}", f"rd{plan}@example.com")
        ok, message = can_connect_integration(store, account, "reddit")
        assert not ok and "free access" in message


def test_x_connect_start_allowed_for_starter(store, config):
    cfg = replace(config, x_client_id="x-id", x_client_secret="x-secret",
                  x_app_registered=True)
    tclient = TestClient(create_app(saas_store=store, saas_config=cfg))
    account = _tier_account(store, "starter", "x-r", "xr@example.com")
    _login(tclient, store, account)
    site = _site(store, account["id"], "https://xr.example.com")
    r = tclient.get(f"/websites/sites/{site['id']}/connections/x/start",
                    follow_redirects=False)
    assert r.status_code == 303  # past the plan gate: off to X


def test_starter_refusal_names_what_it_includes(store):
    account = _tier_account(store, "starter", "x-m", "xm@example.com")
    ok, message = can_connect_integration(store, account, "facebook")
    assert not ok and "posts to X" in message and "Growth or Pro" in message
    ok, message = can_connect_integration(store, account, "tiktok")
    assert not ok and "TikTok needs Pro" in message


def test_x_connect_start_allowed_for_growth(store, config):
    cfg = replace(config, x_client_id="x-id", x_client_secret="x-secret",
                  x_app_registered=True)
    tclient = TestClient(create_app(saas_store=store, saas_config=cfg))
    account = _tier_account(store, "growth", "x-g", "xg@example.com")
    _login(tclient, store, account)
    site = _site(store, account["id"], "https://xg.example.com")
    r = tclient.get(f"/websites/sites/{site['id']}/connections/x/start",
                    follow_redirects=False)
    # Past the plan gate: the OAuth redirect to X.
    assert r.status_code == 303
    assert "twitter.com" in r.headers["location"] or "x.com" in r.headers["location"]


# -- API access ---------------------------------------------------------------

def test_api_access_per_plan(store):
    cases = (("starter", False), ("growth", True), ("pro", True))
    for n, (plan, allowed) in enumerate(cases):
        account = _tier_account(store, plan, f"api-{n}", f"api{n}@example.com")
        ok, message = can_use_api(store, account)
        assert ok is allowed
        if not allowed:
            assert "API access is not included" in message
    legacy = _account(store)
    ok, _ = can_use_api(store, legacy)
    assert ok
    tier = _tier_account(store, "starter", "api-op", "apiop@example.com")
    ok, _ = can_use_api(store, tier, is_operator=True)
    assert ok


def test_api_snapshot_gated_for_starter(client, store, config):
    account = _tier_account(store, "starter", "api-s", "apis@example.com")
    _login(client, store, account)
    r = client.get("/api/snapshot")
    assert r.status_code == 403
    assert "API access is not included" in r.json()["detail"]
    store.set_plan(account["id"], "growth")
    r = client.get("/api/snapshot")
    assert r.status_code == 200
    legacy = _account(store)
    _login(client, store, legacy)
    r = client.get("/api/snapshot")
    assert r.status_code == 200


def test_api_report_gated_for_starter(client, store, config):
    account = _tier_account(store, "starter", "api-r", "apir@example.com")
    _login(client, store, account)
    r = client.get("/api/report")
    assert r.status_code == 403


# -- autopilot past the cap is visible, never silently on ------------------------

def test_new_site_past_the_agent_cap_starts_with_autopilot_off(client, store):
    account = _tier_account(store, "starter", "ap-new", "apnew@example.com")
    _login(client, store, account)
    for n in range(3):
        client.post("/websites/sites", data={"url": f"https://ap{n}.example.com"},
                    follow_redirects=False)
    sites = store.list_sites(account["id"])
    assert [int(s["autopilot"]) for s in sites] == [1, 1, 0]


def test_site_over_the_cap_reads_paused(client, store):
    account = _tier_account(store, "starter", "ap-p", "app@example.com")
    _login(client, store, account)
    sites = [_site(store, account["id"], f"https://p{n}.example.com") for n in range(3)]
    # all three on (as after a downgrade): the newest is past Starter's 2
    page = client.get(f"/websites/sites/{sites[2]['id']}").text
    assert "Paused by your plan" in page and ">Paused<" in page
    page = client.get(f"/websites/sites/{sites[0]['id']}").text
    assert "Paused by your plan" not in page and ">On<" in page


def test_subscribe_page_lists_channels_analytics_and_api(client, store, config):
    account = _account(store)
    _login(client, store, account)
    page = client.get("/account/subscribe").text
    for line in ("Posts to X<", "Posts to X, Facebook and Instagram<",
                 "Posts to X, Facebook, Instagram and TikTok<",
                 "Basic analytics (the funnel)", "No API access",
                 "Read-only API access", "Full API access"):
        assert line in page, line

"""Ventoryn professional unification (2026-11-08).

One consistent look across the whole platform, like Aegis — the
same shell, sidebar and header on every signed-in page — and no
game design: the professional Overview keeps the top summary, the
analyze box, the websites and the Live activity feed; agents live
only on /agents; the only way to stop the agents working is the
operator-global pause (Overview button + Settings control).
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from ecosystem.runtime import build_runtime
from ventoryn.app import create_app
from ventoryn.saas import SaasConfig, SaasStore

from _signin import (
    OPERATOR_EMAIL,
    operator_store_and_config,
    sign_in,
)

CUSTOMER_A = ("ada@example.com", "google-sub-ada", "Ada")
ADMIN_PASSWORD = "admin-secret"


def _app_with(admin_password=ADMIN_PASSWORD, **kwargs):
    store, config = operator_store_and_config(
        admin_password=admin_password)
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     audit=AuditLog(),
                     saas_store=store, saas_config=config, **kwargs)
    return app, store


def _customer_client(app, store, who=CUSTOMER_A):
    email, sub, name = who
    client = TestClient(app)
    account = sign_in(client, store, email=email, sub=sub, name=name)
    return client, account


def _operator_client(app, store):
    client = TestClient(app)
    sign_in(client, store)
    return client


def _admin_client(app):
    client = TestClient(app)
    r = client.post("/admin/login",
                    data={"password": ADMIN_PASSWORD})
    assert r.status_code == 200
    return client


def _register_agents(app):
    """Mirror what ecosystem.runtime does: register the real agent
    classes on the bare orchestrator create_app builds for tests."""
    from ecosystem.runtime import AGENT_CLASSES

    orch = app.state.ecosystem["orchestrator"]
    for cls in AGENT_CLASSES:
        orch.registry.register(
            cls.agent_type, capabilities=list(cls.capabilities),
            description=cls.description)


# ---------------------------------------------------------------------------
# 1. Overview — professional, no game
# ---------------------------------------------------------------------------

def test_overview_has_ring_stats_analyze_websites_and_feed():
    app, store = _app_with()
    client, account = _customer_client(app, store)
    store.create_site(account["id"], "https://ada.example.com")
    r = client.get("/ops")
    assert r.status_code == 200
    assert "class='app-sidebar'" in r.text
    # The top summary: ring + verified/pending stats.
    assert "Verified" in r.text
    assert "Pending" in r.text
    assert "Unattributed" in r.text
    assert "Acquisition trend" in r.text
    # The analyze box + the websites list + the Live activity feed.
    assert 'id="analyze-panel"' in r.text
    assert 'id="analyze-url"' in r.text
    assert "ada.example.com" in r.text
    assert "Live activity" in r.text


def test_overview_has_no_game_markers():
    app, store = _app_with()
    client, _account = _customer_client(app, store)
    r = client.get("/ops")
    assert r.status_code == 200
    # The section markers are checked in the page content (after
    # the sidebar), so the sidebar's "Growth Plan" card and the
    # stylesheet comments cannot trip them.
    content = r.text.split("</aside>", 1)[1]
    for marker in ("Your crew", "What the team is doing",
                   "What just happened", "Experiment 001",
                   'id="ops-feed"', 'id="ops-bars"',
                   'id="ops-activity"', 'id="ops-status"',
                   'id="controls"'):
        assert marker not in content, marker
    # No game artifacts anywhere on the page, and no runtime
    # controls at all on Overview.
    for marker in ("confetti", "game.html", "TICK", "LIVE",
                   "/api/tick", "/api/reset"):
        assert marker not in r.text, marker


def test_game_redirects_to_ops():
    app, store = _app_with()
    client, _account = _customer_client(app, store)
    r = client.get("/game", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ops"
    # Unsigned: still sent to the landing page, not to /ops.
    anon = TestClient(app)
    r = anon.get("/game", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_agents_roster_only_on_agents():
    app, store = _app_with()
    _register_agents(app)
    client, _account = _customer_client(app, store)
    r = client.get("/agents")
    assert r.status_code == 200
    for name in ("Scout", "Analyst", "Studio", "Growth", "Care",
                 "Ops", "Review"):
        assert name in r.text, name
    # The moved sections live here too.
    assert "What the team is doing" in r.text
    assert "What just happened" in r.text
    # ...and nowhere else. ("Ops" is left out of the names: it
    # is the page title "Live Ops". The check runs on the page
    # content after the sidebar, so the sidebar's "Growth Plan"
    # card cannot trip it.)
    for path in ("/ops", "/portfolio", "/campaigns", "/analytics",
                 "/customers", "/integrations"):
        r = client.get(path)
        assert r.status_code == 200, path
        content = r.text.split("</aside>", 1)[1]
        for name in ("Scout", "Analyst", "Studio", "Growth", "Care",
                     "Review"):
            assert name not in content, (path, name)
        assert "Your crew" not in content, path
        assert "What the team is doing" not in content, path


def test_portfolio_has_analyze_box_and_add_site():
    app, store = _app_with()
    client, _account = _customer_client(app, store)
    r = client.get("/portfolio")
    assert r.status_code == 200
    assert 'id="analyze-panel"' in r.text
    assert 'id="analyze-url"' in r.text
    assert "Add a website" in r.text


# ---------------------------------------------------------------------------
# 4. Operator /report — platform shell
# ---------------------------------------------------------------------------

def test_operator_report_has_sidebar_and_no_pill_nav():
    app, store = _app_with()
    client = _operator_client(app, store)
    r = client.get("/report")
    assert r.status_code == 200
    assert "class='app-sidebar'" in r.text
    assert "Agent &amp; Financial Report" in r.text
    assert "class='customer-nav'" not in r.text
    assert "v-burger" not in r.text
    # The report still links to Live Ops through the sidebar.
    assert "Overview" in r.text


# ---------------------------------------------------------------------------
# 5. Customer 2FA pages — platform shell
# ---------------------------------------------------------------------------

def test_two_factor_pages_have_sidebar():
    from _signin import sign_in_raw
    from ventoryn.totp import code_at

    app, store = _app_with()
    client = TestClient(app)
    account = sign_in_raw(client, store, email=CUSTOMER_A[0],
                          sub=CUSTOMER_A[1], name=CUSTOMER_A[2])
    # The setup page is reachable before 2FA is on.
    r = client.get("/account/two-factor/setup")
    assert r.status_code == 200
    assert "class='app-sidebar'" in r.text
    assert "Two-factor" in r.text
    # Complete setup, sign out, sign back in raw: the verify page
    # is the gate, and it renders inside the shell too.
    secret = store.get_totp_pending_secret(account["id"])
    r = client.post("/account/two-factor/setup",
                    data={"code": code_at(secret), "next": "/ops"})
    assert r.status_code == 200
    client.get("/account/logout")
    sign_in_raw(client, store, email=CUSTOMER_A[0],
                sub=CUSTOMER_A[1], name=CUSTOMER_A[2])
    r = client.get("/account/two-factor/verify")
    assert r.status_code == 200
    assert "class='app-sidebar'" in r.text
    assert "Two-factor" in r.text


# ---------------------------------------------------------------------------
# 6. Admin pages — platform shell with the Admin group
# ---------------------------------------------------------------------------

def test_admin_pages_have_sidebar_and_admin_group():
    app, _store = _app_with()
    admin = _admin_client(app)
    for path in ("/admin", "/admin/subscribers", "/admin/accounts",
                 "/admin/agents", "/admin/two-factor/setup"):
        r = admin.get(path)
        assert r.status_code == 200, path
        assert "class='app-sidebar'" in r.text, path
        assert ">Admin<" in r.text, path
        assert "admin-head" not in r.text, path


def test_admin_login_stays_bare():
    app, _store = _app_with()
    r = TestClient(app).get("/admin/login")
    assert r.status_code == 200
    assert "class='app-sidebar'" not in r.text


# ---------------------------------------------------------------------------
# 8. Error pages — platform shell
# ---------------------------------------------------------------------------

def test_unknown_url_renders_shell_404_when_signed_in():
    app, store = _app_with()
    client, _account = _customer_client(app, store)
    r = client.get("/no-such-page")
    assert r.status_code == 404
    assert "Page not found" in r.text
    assert "class='app-sidebar'" in r.text
    assert "detail" not in r.text  # not FastAPI's bare JSON


def test_unknown_url_is_bare_when_unsigned():
    app, _store = _app_with()
    r = TestClient(app).get("/no-such-page")
    assert r.status_code == 404
    assert "Page not found" in r.text
    assert "class='app-sidebar'" not in r.text


# ---------------------------------------------------------------------------
# Pause — the only way to stop the agents working
# ---------------------------------------------------------------------------

def test_pause_flag_persists_across_store_reopen(tmp_path):
    db = tmp_path / "state.db"
    store = SaasStore(str(db))
    assert store.get_global_pause() is False
    store.set_global_pause(True)
    assert store.get_global_pause() is True
    # A fresh store on the same file sees the same flag — it
    # survives a dashboard restart.
    reopened = SaasStore(str(db))
    assert reopened.get_global_pause() is True
    reopened.set_global_pause(False)
    assert SaasStore(str(db)).get_global_pause() is False


def test_paused_tick_is_refused_and_counter_unchanged():
    app, store = _app_with()
    client = _operator_client(app, store)
    tick_before = client.get("/api/snapshot").json()["tick"]
    r = client.post("/api/tick")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    tick_after = client.get("/api/snapshot").json()["tick"]
    assert tick_after == tick_before + 1
    # Pause, then a tick is refused and the counter does not move.
    r = client.post("/api/pause", data={"paused": "1", "next": "/ops"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ops"
    assert store.get_global_pause() is True
    r = client.post("/api/tick")
    assert r.status_code == 409
    assert r.json()["ok"] is False
    assert client.get("/api/snapshot").json()["tick"] == tick_after
    # Unpause: ticks run again.
    r = client.post("/api/pause", data={"paused": "0", "next": "/ops"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert store.get_global_pause() is False
    r = client.post("/api/tick")
    assert r.status_code == 200
    assert client.get("/api/snapshot").json()["tick"] == tick_after + 1


def test_pause_toggle_is_operator_only():
    app, store = _app_with()
    client, _account = _customer_client(app, store)
    r = client.post("/api/pause", data={"paused": "1", "next": "/ops"})
    assert r.status_code == 403
    assert store.get_global_pause() is False


def test_pause_button_reflects_state_on_ops_and_dashboard():
    app, store = _app_with()
    client = _operator_client(app, store)
    # Running: the button offers to pause.
    for path in ("/ops", "/account/dashboard"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Pause all agents" in r.text, path
        assert "Resume all agents" not in r.text, path
        assert "Agents are running." in r.text, path
    # Paused: the button offers to resume.
    store.set_global_pause(True)
    for path in ("/ops", "/account/dashboard"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Resume all agents" in r.text, path
        assert "Pause all agents" not in r.text, path
        assert "Agents are paused." in r.text, path


def test_pause_does_not_block_customer_analyze():
    """On-demand analyze jobs are untouched by the pause."""
    app, store = _app_with()
    store.set_global_pause(True)
    client = _operator_client(app, store)
    client.app.state.analyze_runner = lambda url, country, language: {
        "site_score": 90, "geo_score": 80, "fetchable": True,
        "unavailable_reason": None,
        "finding_counts": {"critical": 0, "high": 0, "medium": 0,
                           "low": 0},
        "findings": [], "report_name": "analyze_example-com_20261108.md",
    }
    r = client.post("/api/analyze",
                    json={"url": "https://example.com", "country": "ca"})
    assert r.status_code == 202
    assert "job_id" in r.json()


def test_worker_skips_all_tick_work_while_paused(tmp_path, monkeypatch,
                                                 capsys):
    """The background worker loop reads the same persisted flag and
    skips growth, discovery, monitor and store work while paused."""
    from ecosystem import worker

    db = tmp_path / "state.db"
    store = SaasStore(str(db))
    store.set_global_pause(True)
    monkeypatch.setenv("ECOSYSTEM_STATE_DB", str(db))
    rt = build_runtime(use_llm=False)
    with patch.object(worker, "run_growth_tick") as g, \
         patch.object(worker, "run_tick") as d, \
         patch.object(worker, "run_store_tick") as s, \
         patch.object(worker, "run_etsy_monitor_tick") as m:
        rc = worker.main(["--ticks", "1", "--interval", "0",
                          "--monitor-every", "1", "--store"], rt=rt)
    assert rc == 0
    assert not g.called and not d.called and not s.called
    assert not m.called
    assert "paused — skipping tick work" in capsys.readouterr().out


def test_worker_runs_tick_work_when_not_paused(tmp_path, monkeypatch):
    from ecosystem import worker

    db = tmp_path / "state.db"
    SaasStore(str(db))  # flag defaults to off
    monkeypatch.setenv("ECOSYSTEM_STATE_DB", str(db))
    rt = build_runtime(use_llm=False)
    with patch.object(worker, "run_growth_tick") as g, \
         patch.object(worker, "run_tick") as d, \
         patch.object(worker, "run_store_tick") as s:
        rc = worker.main(["--ticks", "1", "--interval", "0",
                          "--monitor-every", "0"], rt=rt)
    assert rc == 0
    assert g.called and not d.called and not s.called

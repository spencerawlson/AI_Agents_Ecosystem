"""Platform sidebar pages (Ventoryn UI integration).

The five new sidebar pages — /campaigns, /agents, /analytics,
/integrations, /customers — plus the sidebar contract on every
signed-in page: all 9 items present, current page non-clickable,
vector-only (no bitmaps, no font-glyph arrows), honest empty
states (never demo numbers), and the additive snapshot attribution
block.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from ventoryn.app import create_app
from ventoryn.saas import SaasConfig
from orchestrator.approvals import ApprovalGate

from _signin import (
    operator_store_and_config,
    sign_in,
)

CUSTOMER_A = ("ada@example.com", "google-sub-ada", "Ada")
CUSTOMER_B = ("ben@example.com", "google-sub-ben", "Ben")
ADMIN_PASSWORD = "test-admin-password"

NEW_PAGES = ("/campaigns", "/agents", "/analytics",
             "/integrations", "/customers")
SIDEBAR_PAGES = ("/ops", "/portfolio", "/approvals", "/report",
                 "/account/dashboard", "/account/subscribe") + NEW_PAGES
SIDEBAR_ITEMS = ("Overview", "Campaigns", "Agents", "Approvals",
                 "Attribution", "Analytics", "Integrations",
                 "Customers", "Settings")
# Font-glyph arrows / icon glyphs / emoji that must never appear in
# server-rendered platform HTML (the "92" bug came from \2192).
BANNED_GLYPHS = ("→", "←", "✓", "☰", "🔍", "·", "»", "▸", "⌄",
                 "\u2192", "\u2713")
BITMAP_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico")


def _app_with(admin_password=ADMIN_PASSWORD, approvals=None):
    store, config = operator_store_and_config(
        admin_password=admin_password)
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     approvals=approvals or ApprovalGate(),
                     audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store


def _customer_client(app, store, who):
    email, sub, name = who
    client = TestClient(app)
    account = sign_in(client, store, email=email, sub=sub, name=name)
    return client, account


def _operator_client(app, store):
    client = TestClient(app)
    account = sign_in(client, store)
    return client, account


def _sidebar_aside(text: str) -> str:
    return text.split("<aside class='app-sidebar'")[1].split("</aside>")[0]


# ---------------------------------------------------------------------------
# 1. New pages: auth + sidebar + honest empty states
# ---------------------------------------------------------------------------

def test_new_pages_200_signed_in_303_unsigned():
    app, store = _app_with()
    anon = TestClient(app)
    for client, who in ((_customer_client(app, store, CUSTOMER_A)[0], "a"),
                        (_operator_client(app, store)[0], "op")):
        for path in NEW_PAGES:
            r = client.get(path)
            assert r.status_code == 200, (who, path)
            assert "class='app-sidebar'" in r.text, (who, path)
    for path in NEW_PAGES:
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/", path


def test_sidebar_all_nine_items_on_every_signed_in_page():
    app, store = _app_with()
    clients = ((_customer_client(app, store, CUSTOMER_A)[0], False),
               (_operator_client(app, store)[0], True))
    for client, is_operator in clients:
        for path in SIDEBAR_PAGES:
            r = client.get(path)
            assert r.status_code == 200, path
            aside = _sidebar_aside(r.text)
            for item in SIDEBAR_ITEMS:
                assert item in aside, (path, item)
            # The current page (when it is a sidebar item) is a
            # non-clickable span, never a dead link. /portfolio maps
            # to Overview; /account/subscribe is not a sidebar
            # item, so nothing is marked current there.
            assert "aria-current='page'" in aside or path in (
                "/account/subscribe",), path
    # The operator's /report renders through the platform shell too
    # (step 4 of the unification): sidebar, no pill nav.
    op_client, _op = _operator_client(app, store)
    r = op_client.get("/report")
    assert r.status_code == 200
    assert "class='app-sidebar'" in r.text
    assert "class='customer-nav'" not in r.text
    assert "v-burger" not in r.text


def test_new_pages_honest_empty_states_no_demo_numbers():
    app, store = _app_with()
    client, _account = _customer_client(app, store, CUSTOMER_A)
    # A fresh account: no sites, no campaigns, no conversions.
    # Pages with no sites say so first; the per-page empty states
    # appear once a site exists (see the seeded test below).
    expected = {
        "/campaigns": "No websites yet",
        "/agents": "No agents registered yet",
        "/analytics": "No websites yet",
        "/integrations": "No websites yet",
        "/customers": "No paying customers recorded yet",
    }
    for path, text in expected.items():
        r = client.get(path)
        assert r.status_code == 200, path
        assert text in r.text, (path, text)
    # Nothing on an empty account looks like traction.
    for path in NEW_PAGES:
        r = client.get(path)
        assert "24 / 100" not in r.text, path
        assert "24 of 100" not in r.text, path


def test_new_pages_vector_only_no_glyph_arrows():
    app, store = _app_with()
    client, _account = _customer_client(app, store, CUSTOMER_A)
    for path in NEW_PAGES + ("/ops", "/report", "/approvals"):
        r = client.get(path)
        assert r.status_code == 200, path
        for glyph in BANNED_GLYPHS:
            assert glyph not in r.text, (path, repr(glyph))
        for ext in BITMAP_EXTS:
            # A bitmap reference is a file URL: the extension must
            # be followed by a quote, whitespace, ? or # — this
            # keeps JS identifiers such as `c.icon` from matching
            # ".ico".
            assert not re.search(
                r"\." + re.escape(ext.lstrip(".")) + r"[\"'\s?#>#]",
                r.text.lower()), (path, ext)
        # The broken-arrow symptom: a bare "92" between cards.
        assert ">92<" not in r.text, path
        assert "\\2192" not in r.text, path
        assert "\\2713" not in r.text, path


# ---------------------------------------------------------------------------
# 2. Real data on the new pages (seeded account)
# ---------------------------------------------------------------------------

def _register_agents(app):
    """Mirror what ecosystem.runtime does: register the real agent
    classes on the bare orchestrator create_app builds for tests
    (otherwise the registry is empty and /agents honestly reports
    "No agents registered yet")."""
    from ecosystem.runtime import AGENT_CLASSES

    orch = app.state.ecosystem["orchestrator"]
    for cls in AGENT_CLASSES:
        orch.registry.register(
            cls.agent_type, capabilities=list(cls.capabilities),
            description=cls.description)


def test_agents_lists_the_real_registry_roster():
    app, store = _app_with()
    _register_agents(app)
    client, _account = _customer_client(app, store, CUSTOMER_A)
    r = client.get("/agents")
    assert r.status_code == 200
    # The real agent types from the orchestrator registry — the
    # crew cast names, not the reference's four demo agents.
    for name in ("Scout", "Analyst", "Studio", "Growth", "Care",
                 "Ops", "Review"):
        assert name in r.text, name
    assert "No agents registered yet" not in r.text
    # Live status comes from the audit log: nothing has run yet.
    assert "Standing by" in r.text


def test_verified_trend_uses_utc_today(monkeypatch):
    """A verified sale made after 19:00 EST is dated the next day in
    UTC, and the trend's last point is that UTC today — never 0.
    Regression: the trend used the local date for "today" while
    conversions are timestamped in UTC, so the last point read 0
    every evening after 19:00 EST."""
    from datetime import datetime, timezone

    import ventoryn.saas as saas_module

    app, store = _app_with()
    client, account = _customer_client(app, store, CUSTOMER_A)
    site = store.create_site(account["id"], "https://ada.example.com")
    # 01:30 UTC on Nov 9 == 20:30 EST on Nov 8.
    fixed_utc = datetime(2026, 11, 9, 1, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(saas_module, "_now", lambda: fixed_utc)
    store.record_conversion(site["id"], 2000, "usd", "stripe",
                            stripe_event_id="evt-verified",
                            attribution="ventoryn",
                            signature_tag="vtn_act-1", action_id="act-1")
    trend = store.verified_trend([site["id"]], days=30)
    assert trend[-1] == {"date": "2026-11-09", "verified": 1}


def test_campaigns_customers_analytics_show_seeded_conversions():
    app, store = _app_with()
    client, account = _customer_client(app, store, CUSTOMER_A)
    site = store.create_site(account["id"], "https://ada.example.com")
    # One signed campaign (a signature) + three conversions in the
    # three attribution states.
    store.record_conversion(site["id"], 2000, "usd", "stripe",
                            stripe_event_id="evt-verified",
                            attribution="ventoryn",
                            signature_tag="vtn_act-1", action_id="act-1")
    store.record_conversion(site["id"], 5000, "usd", "manual")
    store.record_conversion(site["id"], 3000, "usd", "stripe",
                            stripe_event_id="evt-unattributed")
    r = client.get("/campaigns")
    assert r.status_code == 200
    assert "No signed campaigns yet" in r.text  # no signatures yet
    r = client.get("/customers")
    assert r.status_code == 200
    assert "No paying customers recorded yet" not in r.text
    assert "Verified" in r.text and "Pending" in r.text
    assert "Unattributed" in r.text
    r = client.get("/analytics")
    assert r.status_code == 200
    assert "unavailable — signup tracking is not connected yet" in r.text
    # The snapshot carries the same attribution block the pages use.
    data = client.get("/api/snapshot").json()
    attr = data["attribution"]
    assert attr["counts"]["verified"] == 1
    assert attr["counts"]["pending"] == 1
    assert attr["counts"]["unattributed"] == 1
    assert attr["counts"]["total"] == 3
    assert attr["verified"] == 1
    assert len(attr["flow_steps"]) == 6
    # verified_trend(days=30) returns start..today inclusive: 31
    # points once there is at least one verified sale.
    assert len(attr["trend"]) == 31
    assert attr["trend"][-1]["verified"] == 1


def test_integrations_states_plainly_reddit_x_need_app_registration():
    app, store = _app_with()
    client, account = _customer_client(app, store, CUSTOMER_A)
    store.create_site(account["id"], "https://ada.example.com")
    r = client.get("/integrations")
    assert r.status_code == 200
    assert "not connected — app registration needed" in r.text
    # The honesty note spans a line break in the HTML, so assert
    # the two halves separately.
    assert "X's API" in r.text
    assert "paid and rate-limited" in r.text
    for kind in ("Reddit", "X", "Plausible", "Stripe"):
        assert kind in r.text, kind


# ---------------------------------------------------------------------------
# 3. /ops Overview: real data, sidebar, no demo numbers
# ---------------------------------------------------------------------------

def test_ops_overview_real_empty_state():
    app, store = _app_with()
    client, _account = _customer_client(app, store, CUSTOMER_A)
    r = client.get("/ops")
    assert r.status_code == 200
    assert "class='app-sidebar'" in r.text
    # The Overview cards are present with real (empty) numbers.
    for label in ("Verified", "Pending", "Unattributed",
                  "Attribution flow", "Conversion status",
                  "Campaign performance", "Acquisition trend",
                  "Conversion breakdown"):
        assert label in r.text, label
    # No crew on Overview: agents live only on /agents.
    assert "Your crew" not in r.text
    assert "No signed campaigns yet" in r.text
    # The analyze panel is still there.
    assert "analyze-url" in r.text
    assert "analyze-submit" in r.text


def test_snapshot_attribution_is_additive_and_empty_for_new_account():
    app, store = _app_with()
    client, _account = _customer_client(app, store, CUSTOMER_A)
    data = client.get("/api/snapshot").json()
    # The pre-existing snapshot keys are untouched.
    for key in ("tick", "agents", "businesses", "events", "totals",
                "account"):
        assert key in data, key
    attr = data["attribution"]
    assert attr["counts"] == {"verified": 0, "pending": 0,
                              "unattributed": 0, "total": 0}
    assert attr["verified"] == 0
    assert attr["last7"] == 0
    assert attr["campaigns"] == []
    assert len(attr["flow_steps"]) == 6
    assert all(step["ok"] is False for step in attr["flow_steps"])

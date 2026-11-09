"""Nav polish + admin refinement (Ventoryn).

- One shared platform shell on every signed-in page: a purple
  sidebar (9 items, in order: Overview, Campaigns, Agents,
  Approvals, Attribution, Analytics, Integrations, Customers,
  Settings) + an app header with a mobile toggle and an avatar
  dropdown. The hamburger menu and the pill nav row are both
  gone — the customer /report was rebuilt per the attribution
  reference (image 3) and uses the shared sidebar too; only the
  OPERATOR's agent & financial report keeps the old shell
  untouched, and /admin keeps its own simple header.
- /portfolio in the Live Ops visual language (color).
- Approvals decide redirects back with a confirmation; decided
  items leave pending and show under Decided with the time.
- My account: plan name, websites, recent jobs only — no Subscribe
  pitch, no credit table. The operator sees "Permanent — owner".
- /admin needs the password (VENTORYN_ADMIN_PASSWORD), shows who
  is live now (5-minute window) and recent activity, in the
  Kay Ayiti + ShipItAyiti admin look.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import ventoryn.nav as nav
from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from ventoryn.saas import SaasConfig, SaasStore
from ventoryn.app import create_app
from orchestrator.approvals import ApprovalGate

from _signin import (
    OPERATOR_EMAIL,
    operator_store_and_config,
    sign_in,
)

CUSTOMER_A = ("ada@example.com", "google-sub-ada", "Ada")
CUSTOMER_B = ("ben@example.com", "google-sub-ben", "Ben")
ADMIN_PASSWORD = "test-admin-password"


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


def _admin_client(app):
    """A client holding a valid admin password session."""
    client = TestClient(app)
    r = client.post("/admin/login", data={"password": ADMIN_PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"
    return client


# ---------------------------------------------------------------------------
# 1. The hamburger header replaces the pill nav row
# ---------------------------------------------------------------------------

SIGNED_IN_PAGES = ("/ops", "/portfolio", "/approvals",
                   "/account/dashboard", "/account/subscribe")


def test_no_pill_nav_on_signed_in_pages():
    app, store = _app_with()
    clients = [_customer_client(app, store, CUSTOMER_A)[0],
               _operator_client(app, store)[0]]
    for client in clients:
        for path in SIGNED_IN_PAGES:
            r = client.get(path)
            assert r.status_code == 200, path
            # The pill row is gone (both spellings: the server pages
            # and the Live Ops page).
            assert "class='customer-nav'" not in r.text, path
            assert 'id="customer-nav"' not in r.text, path
            # The purple sidebar is there instead of the hamburger.
            assert "class='app-sidebar'" in r.text, path
            assert "class='app-header'" in r.text, path
            assert "sidebar-toggle" in r.text, path
            assert "v-burger" not in r.text, path
            assert "v-header" not in r.text, path


def test_sidebar_brand_links_home_and_toggle_is_thumb_sized():
    # The sidebar brand links home (/ops); the mobile toggle is a
    # thumb-sized 44px button; there is no hamburger anywhere.
    app, store = _app_with()
    client_a, _a = _customer_client(app, store, CUSTOMER_A)
    r = client_a.get("/portfolio")
    assert r.status_code == 200
    assert "class='app-sidebar'" in r.text
    assert "href='/ops'" in r.text  # brand + Overview links
    assert "sidebar-toggle" in r.text
    assert "44px" in r.text
    assert "v-burger" not in r.text


def test_sidebar_items_per_role():
    app, store = _app_with()
    client_a, _a = _customer_client(app, store, CUSTOMER_A)
    r = client_a.get("/portfolio")
    for item in ("Overview", "Campaigns", "Agents", "Approvals",
                 "Attribution", "Analytics", "Integrations",
                 "Customers", "Settings", "Sign out"):
        assert item in r.text, item
    # My account + Subscribe live in the avatar dropdown, not the
    # sidebar — but they are still one tap away.
    assert "My account" in r.text
    assert "Subscribe" in r.text
    # A customer never sees the Admin group.
    assert "<span class='nav-group'>Admin</span>" not in r.text
    assert "/admin/subscribers" not in r.text

    client_op, _op = _operator_client(app, store)
    r = client_op.get("/portfolio")
    for item in ("Overview", "Campaigns", "Agents", "Approvals",
                 "Attribution", "Analytics", "Integrations",
                 "Customers", "Settings", "Sign out"):
        assert item in r.text, item
    # The operator gets the Admin group on top of the customer menu.
    assert "<span class='nav-group'>Admin</span>" in r.text
    for item in ("Live", "Subscribers", "Accounts", "Agents"):
        assert item in r.text, item
    assert "/admin/subscribers" in r.text
    assert "/admin/accounts" in r.text
    assert "/admin/agents" in r.text


def test_current_page_is_not_a_clickable_menu_option():
    app, store = _app_with()
    client_a, _a = _customer_client(app, store, CUSTOMER_A)
    for path, href, label in (
            ("/ops", "/ops", "Overview"),
            ("/campaigns", "/campaigns", "Campaigns"),
            ("/agents", "/agents", "Agents"),
            ("/approvals", "/approvals", "Approvals"),
            ("/report", "/report", "Attribution"),
            ("/analytics", "/analytics", "Analytics"),
            ("/integrations", "/integrations", "Integrations"),
            ("/customers", "/customers", "Customers"),
            ("/account/dashboard", "/account/dashboard", "Settings")):
        r = client_a.get(path)
        assert r.status_code == 200, path
        # The current page is a label, never a link to itself.
        assert f"<a class='nav-item' href='{href}'>" not in r.text, \
            (path, href)
        assert label in r.text, (path, label)
        assert "aria-current='page'" in r.text, path
    # /portfolio is not a sidebar item: nothing is marked current.
    r = client_a.get("/portfolio")
    assert r.status_code == 200
    assert "aria-current='page'" not in r.text
    # The admin pages too: the current admin page is a label.
    admin = _admin_client(app)
    r = admin.get("/admin/subscribers")
    assert r.status_code == 200
    assert "<a href='/admin/subscribers'>" not in r.text
    assert "Subscribers" in r.text
    assert "aria-current='page'" in r.text


def test_sidebar_js_syntax():
    # The sidebar script is plain vanilla JS — it must parse cleanly.
    if shutil.which("node") is None:
        pytest.skip("node is not available here")
    script = nav.SIDEBAR_JS
    assert script.strip().startswith("<script>")
    js = re.sub(r"</?script>", "", script)
    with tempfile.NamedTemporaryFile("w", suffix=".js",
                                    delete=False) as fh:
        fh.write(js)
        name = fh.name
    try:
        proc = subprocess.run(["node", "--check", name],
                              capture_output=True, text=True, timeout=30)
    finally:
        Path(name).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# 2. Portfolio color
# ---------------------------------------------------------------------------

def test_portfolio_carries_live_ops_color():
    registry = BusinessRegistry()
    ledger = Ledger()
    biz1 = registry.create("Ops Store", "ecommerce")
    ledger.record(biz1.id, "revenue", 5000)
    registry.create("Second Store", "ecommerce")
    store, config = operator_store_and_config(
        admin_password=ADMIN_PASSWORD)
    app = create_app(registry=registry, ledger=ledger,
                     approvals=ApprovalGate(), audit=AuditLog(),
                     saas_store=store, saas_config=config)
    client_op, _op = _operator_client(app, store)
    r = client_op.get("/portfolio")
    assert r.status_code == 200
    assert "v-stats" in r.text
    assert "v-stat-1" in r.text and "v-stat-2" in r.text
    assert "v-stat-3" in r.text
    assert "linear-gradient" in r.text  # violet/gradient stat cards
    assert "v-biz-grid" in r.text
    # Per-business accent: two businesses, two different accents.
    assert "v-accent-0" in r.text
    assert "v-accent-1" in r.text
    assert "Ops Store" in r.text
    assert "Second Store" in r.text


# ---------------------------------------------------------------------------
# 3. Approvals decide flow
# ---------------------------------------------------------------------------

def test_approvals_reject_leaves_pending_and_shows_decided():
    registry = BusinessRegistry()
    biz = registry.create("Ops Store", "ecommerce")
    gate = ApprovalGate()
    gate.request("ad_spend_increase", amount_usd=100.0, business_id=biz.id)
    app, store = _app_with(approvals=gate)
    client_op, _op = _operator_client(app, store)
    appr = gate.pending()[0]
    r = client_op.post(f"/approvals/{appr.id}/decide",
                       data={"approved": "false"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/approvals?decided={appr.id}"
    assert len(gate.pending()) == 0
    r = client_op.get(f"/approvals?decided={appr.id}")
    assert r.status_code == 200
    assert "Rejected — nothing ships without your say-so." in r.text
    assert "Pending (0)" in r.text
    # The decided item is under Decided with its status and time.
    assert "Decided" in r.text
    assert "rejected" in r.text
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", r.text), r.text
    # …and it is no longer in the pending list.
    assert "ad_spend_increase" in r.text  # still visible, under Decided
    assert "Nothing awaiting approval." in r.text


def test_approvals_ownership_403_still_holds():
    registry = BusinessRegistry()
    biz = registry.create("Ops Store", "ecommerce")
    gate = ApprovalGate()
    gate.request("ad_spend_increase", amount_usd=100.0, business_id=biz.id)
    app, store = _app_with(approvals=gate)
    client_a, _a = _customer_client(app, store, CUSTOMER_A)
    appr = gate.pending()[0]
    r = client_a.post(f"/approvals/{appr.id}/decide",
                      data={"approved": "true"})
    assert r.status_code == 403
    assert len(gate.pending()) == 1  # still pending


# ---------------------------------------------------------------------------
# 4. My account de-duplication + owner permanent subscription
# ---------------------------------------------------------------------------

def test_subscribe_page_still_has_pitch_and_credit_table():
    # Stripe configured, so the checkout button is really there.
    store = SaasStore(":memory:")
    config = SaasConfig(operator_email=OPERATOR_EMAIL,
                        admin_password=ADMIN_PASSWORD,
                        stripe_secret_key="sk_test_shell",
                        stripe_price_id="price_20usd",
                        base_url="http://testserver")
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     approvals=ApprovalGate(), audit=AuditLog(),
                     saas_store=store, saas_config=config)
    client_a, _a = _customer_client(app, store, CUSTOMER_A)
    r = client_a.get("/account/subscribe")
    assert r.status_code == 200
    assert "$20/month" in r.text
    assert "What a credit buys" in r.text
    assert "Subscribe — $20/month" in r.text


def test_operator_shows_permanent_subscription():
    app, store = _app_with()
    client_op, _op = _operator_client(app, store)
    r = client_op.get("/account/dashboard")
    assert r.status_code == 200
    assert "Permanent — owner" in r.text
    assert "Subscribe — $20/month" not in r.text
    r = client_op.get("/account/subscribe")
    assert r.status_code == 200
    assert "Permanent — owner" in r.text
    assert "Subscribe — $20/month" not in r.text
    # The owner cannot start a checkout either.
    r = client_op.post("/account/subscribe", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/account/dashboard"


def test_customer_my_account_plan_only_no_pitch():
    app, store = _app_with()
    client_a, account_a = _customer_client(app, store, CUSTOMER_A)
    store.create_site(account_a["id"], "https://ada.example.com")
    r = client_a.get("/account/dashboard")
    assert r.status_code == 200
    assert "My account" in r.text
    assert "Ventoryn Growth" in r.text  # the plan name
    assert "ada.example.com" in r.text  # the websites
    assert "Recent jobs" in r.text
    assert "Subscribe — $20/month" not in r.text
    assert "$20/month" not in r.text
    assert "What a credit buys" not in r.text


# ---------------------------------------------------------------------------
# 5. /report: the CUSTOMER report was rebuilt per the attribution
#    reference (image 3) and uses the shared header; only the
#    OPERATOR's agent & financial report keeps the old shell.
# ---------------------------------------------------------------------------

def test_report_header_split_customer_vs_operator():
    app, store = _app_with()
    client_a, account_a = _customer_client(app, store, CUSTOMER_A)
    store.create_site(account_a["id"], "https://ada.example.com")
    r = client_a.get("/report")
    assert r.status_code == 200
    # The customer report: shared sidebar, no pill nav, no hamburger.
    assert "class='app-sidebar'" in r.text
    assert "v-burger" not in r.text
    assert "class='customer-nav'" not in r.text
    assert "ada.example.com" in r.text
    # The operator's agent & financial report keeps its shell.
    client_op, _op = _operator_client(app, store)
    r = client_op.get("/report")
    assert r.status_code == 200
    assert "class='customer-nav'" in r.text
    assert "v-burger" not in r.text
    assert "class='app-sidebar'" not in r.text


# ---------------------------------------------------------------------------
# 6. Admin: password gate, live feed, new look
# ---------------------------------------------------------------------------

def test_admin_session_persists_and_logout_closes_it():
    app, _store = _app_with()
    admin = _admin_client(app)
    assert admin.get("/admin").status_code == 200
    assert admin.get("/admin/subscribers").status_code == 200
    assert admin.get("/admin/accounts").status_code == 200
    assert admin.get("/admin/agents").status_code == 200
    r = admin.get("/admin/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"
    r = admin.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


def test_admin_live_list_shows_recent_hides_stale():
    app, store = _app_with()
    client_a, account_a = _customer_client(app, store, CUSTOMER_A)
    _client_b, account_b = _customer_client(app, store, CUSTOMER_B)
    # A is on the site right now (any authenticated request stamps
    # the last-seen); B was last seen 10 minutes ago — stale.
    client_a.get("/ops")
    store.touch_activity(account_b["id"], "Live Ops")
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10))
    with store._lock:
        store._conn.execute(
            "UPDATE saas_activity SET last_seen_at = ? WHERE account_id = ?",
            (stale.isoformat(), account_b["id"]))
        store._conn.commit()
    admin = _admin_client(app)
    r = admin.get("/admin")
    assert r.status_code == 200
    assert "Live now" in r.text
    assert "active in the last 5 minutes" in r.text
    assert "ada@example.com" in r.text
    assert "ben@example.com" not in r.text
    assert "Live Ops" in r.text  # A's last action


def test_admin_recent_activity_feed_lists_real_events():
    app, store = _app_with()
    client_a, account_a = _customer_client(app, store, CUSTOMER_A)
    store.record_activity_event(account_a["id"], "sign-in",
                                "Signed in with Google")
    store.record_activity_event(account_a["id"], "site-added",
                                "Website added: https://ada.example.com")
    store.record_activity_event(account_a["id"], "subscribed",
                                "$20/month subscription started")
    admin = _admin_client(app)
    r = admin.get("/admin")
    assert r.status_code == 200
    assert "Recent activity" in r.text
    assert "Signed in" in r.text
    assert "Website added" in r.text
    assert "Subscribed" in r.text
    assert "ada@example.com" in r.text
    assert "https://ada.example.com" in r.text
    # And the events are real: nothing is invented for an account
    # that did nothing.
    assert "ben@example.com" not in r.text


def test_admin_pages_carry_the_new_styling():
    app, _store = _app_with()
    admin = _admin_client(app)
    r = admin.get("/admin")
    assert r.status_code == 200
    assert "v-admin-card" in r.text
    assert "admin-head" in r.text  # the simple admin header, no sidebar
    assert "class='app-sidebar'" not in r.text
    assert "v-live-dot" in r.text or "Nobody is on the platform" in r.text
    for path in ("/admin/subscribers", "/admin/accounts", "/admin/agents"):
        r = admin.get(path)
        assert r.status_code == 200, path
        assert "v-admin-card" in r.text, path
        assert "admin-head" in r.text, path
        assert "class='app-sidebar'" not in r.text, path


def test_admin_login_page_is_password_only():
    app, _store = _app_with()
    r = TestClient(app).get("/admin/login")
    assert r.status_code == 200
    assert "Admin password" in r.text
    assert 'type="password"' in r.text or "type='password'" in r.text
    assert "Google" not in r.text
    assert "username" not in r.text.lower()

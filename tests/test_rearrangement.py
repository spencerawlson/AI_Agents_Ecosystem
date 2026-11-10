"""The whole-site rearrangement (approved 2026-11-08).

Sidebar order + labels, the Overview section order with the
"Needs your decision" strip, the analyze box's one home, the My
account sub-nav, the website page section order, expandable
approval rows, /report as a deep link under Analytics, the
Analytics donut, the Customers signature link, the Integrations
Stripe hint, and the Websites page's Operator businesses group.
(The pause pill in the Overview header + the full card in My
account are pinned in test_platform_unification.py.)
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
ADMIN_PASSWORD = "admin-secret-for-tests"

CUSTOMER_ITEMS = ("Overview", "Websites", "Campaigns", "Approvals",
                  "Agents", "Analytics", "Customers", "Integrations",
                  "My account")
ADMIN_ITEMS = ("Live", "Subscribers", "Accounts", "Agent activity")


def _app_with(approvals=None, registry=None):
    store, config = operator_store_and_config(
        admin_password=ADMIN_PASSWORD)
    app = create_app(registry=registry or BusinessRegistry(),
                     ledger=Ledger(),
                     approvals=approvals or ApprovalGate(),
                     audit=AuditLog(),
                     saas_store=store, saas_config=config)
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


def _sidebar_labels(text: str) -> list[str]:
    aside = text.split("<aside class='app-sidebar'")[1].split(
        "</aside>")[0]
    return re.findall(r"class='nav-label'>([^<]+)</span>", aside)


def _seed_customer(store, account, registry=None):
    """One site + one verified conversion for the customer. Returns
    the site dict."""
    site = store.create_site(account["id"], "https://ada.example.com")
    store.record_conversion(site["id"], 1900, "usd", "stripe",
                            stripe_event_id="evt-1",
                            attribution="ventoryn",
                            signature_tag="vtn_act-1",
                            action_id="act-1")
    return site


def _seed_approval(registry, approvals, owner_email):
    """A pending approval on a registry business owned by
    ``owner_email``. Returns (business, approval)."""
    biz = registry.create("Ada Shop", "ecommerce")
    registry.assign_owner(biz.id, owner_email)
    appr = approvals.request("ad_spend_increase", amount_usd=100.0,
                             business_id=biz.id,
                             details={"note": "raise the daily cap"})
    return biz, appr


# ---------------------------------------------------------------------------
# 1. Sidebar order + labels
# ---------------------------------------------------------------------------

def test_sidebar_order_and_labels_customer():
    app, store = _app_with()
    client, _account = _customer_client(app, store)
    r = client.get("/ops")
    assert r.status_code == 200
    labels = _sidebar_labels(r.text)
    assert labels == list(CUSTOMER_ITEMS) + ["Sign out"], labels
    assert "Attribution" not in labels
    assert "Settings" not in labels


def test_sidebar_order_and_labels_operator():
    app, store = _app_with()
    client = _operator_client(app, store)
    r = client.get("/ops")
    assert r.status_code == 200
    labels = _sidebar_labels(r.text)
    assert labels == (list(CUSTOMER_ITEMS) + list(ADMIN_ITEMS)
                      + ["Sign out"]), labels
    # The operator's admin item is "Agent activity", not "Agents" —
    # "Agents" appears exactly once (the customer roster).
    assert labels.count("Agents") == 1
    assert labels.count("Agent activity") == 1


# ---------------------------------------------------------------------------
# 2. /report is a deep link under Analytics
# ---------------------------------------------------------------------------

def test_report_reachable_as_deep_link_under_analytics():
    app, store = _app_with()
    anon = TestClient(app)
    r = anon.get("/report", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    for client in (_customer_client(app, store)[0],
                   _operator_client(app, store)):
        r = client.get("/report")
        assert r.status_code == 200
        labels = _sidebar_labels(r.text)
        assert "Attribution" not in labels
        # Analytics is the current item on /report.
        aside = r.text.split("<aside class='app-sidebar'")[1].split(
            "</aside>")[0]
        assert "aria-current='page'" in aside
        active = re.search(
            r"<span class='nav-item active' aria-current='page'>(.*?)"
            r"</span>", aside, re.S)
        assert active is not None
        assert "Analytics" in active.group(1)


# ---------------------------------------------------------------------------
# 3. Overview section order + decision strip
# ---------------------------------------------------------------------------

def test_overview_section_order_with_decision_strip():
    registry = BusinessRegistry()
    approvals = ApprovalGate()
    app, store = _app_with(approvals=approvals, registry=registry)
    client, account = _customer_client(app, store)
    _seed_customer(store, account)
    _seed_approval(registry, approvals, CUSTOMER_A[0])
    r = client.get("/ops")
    assert r.status_code == 200
    text = r.text
    markers = ("<h2>Verified customers</h2>", "<h2>Needs your decision</h2>",
               "<h2>Your websites</h2>", "<h2>Analyze a site</h2>",
               "<h2>Results</h2>", "<h2>Live activity</h2>",
               "Data &amp; privacy controls</a>")
    positions = [text.index(m) for m in markers]
    assert positions == sorted(positions), markers
    # The strip shows the pending approval inline, with the full
    # action visible before deciding.
    assert "ad_spend_increase" in text
    assert "Full payload" in text
    assert "raise the daily cap" in text
    assert ">Approve</button>" in text
    assert ">Reject</button>" in text
    # The duplicate "Conversion breakdown" card is gone from
    # Overview (it still lives on /report).
    assert "Conversion breakdown" not in text
    # The strip posts to the SAME decide endpoint as /approvals.
    assert "action='/approvals/" in text
    assert "name='next' value='/ops'" in text
    # The website card gained its verified-customers line + View.
    assert "1 verified customer" in text


def test_overview_empty_state_promotes_analyze_first():
    app, store = _app_with()
    client, _account = _customer_client(app, store)
    r = client.get("/ops")
    assert r.status_code == 200
    text = r.text
    # A customer with zero sites sees the analyze box before the
    # websites section — it is the way in.
    assert text.index("Analyze a site") < text.index("Your websites")
    assert "Nothing awaiting approval" in text
    assert "The agents will bring the next draft here." in text


# ---------------------------------------------------------------------------
# 4. The decision strip decides through the real endpoint
# ---------------------------------------------------------------------------

def test_decision_strip_approve_moves_to_decided():
    registry = BusinessRegistry()
    approvals = ApprovalGate()
    app, store = _app_with(approvals=approvals, registry=registry)
    client, _account = _customer_client(app, store)
    _biz, appr = _seed_approval(registry, approvals, CUSTOMER_A[0])
    r = client.post(f"/approvals/{appr.id}/decide",
                    data={"approved": "true", "next": "/ops"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/ops?decided={appr.id}"
    r = client.get(f"/ops?decided={appr.id}")
    assert r.status_code == 200
    assert "Approved — nothing ships without your say-so." in r.text
    assert "Nothing awaiting approval" in r.text
    assert len(approvals.pending()) == 0
    r = client.get("/approvals")
    assert "ad_spend_increase" in r.text
    assert "approved" in r.text


def test_decision_strip_reject():
    registry = BusinessRegistry()
    approvals = ApprovalGate()
    app, store = _app_with(approvals=approvals, registry=registry)
    client, _account = _customer_client(app, store)
    _biz, appr = _seed_approval(registry, approvals, CUSTOMER_A[0])
    r = client.post(f"/approvals/{appr.id}/decide",
                    data={"approved": "false", "next": "/ops"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/ops?decided={appr.id}"
    r = client.get(f"/ops?decided={appr.id}")
    assert "Rejected — nothing ships without your say-so." in r.text
    assert len(approvals.pending()) == 0


def test_decision_strip_cross_account_forbidden():
    registry = BusinessRegistry()
    approvals = ApprovalGate()
    app, store = _app_with(approvals=approvals, registry=registry)
    client_a, _a = _customer_client(app, store, CUSTOMER_A)
    _biz, appr = _seed_approval(registry, approvals, CUSTOMER_B[0])
    # Ada cannot see Ben's approval on her Overview…
    r = client_a.get("/ops")
    assert "ad_spend_increase" not in r.text
    # …and cannot decide it either.
    r = client_a.post(f"/approvals/{appr.id}/decide",
                      data={"approved": "true", "next": "/ops"})
    assert r.status_code == 403
    assert len(approvals.pending()) == 1


# ---------------------------------------------------------------------------
# 5. Analyze box: Overview only
# ---------------------------------------------------------------------------

def test_analyze_box_only_on_overview():
    registry = BusinessRegistry()
    app, store = _app_with(registry=registry)
    client, account = _customer_client(app, store)
    _seed_customer(store, account)
    r = client.get("/ops")
    assert 'id="analyze-panel"' in r.text
    r = client.get("/portfolio")
    assert 'id="analyze-panel"' not in r.text
    op = _operator_client(app, store)
    r = op.get("/businesses")
    assert r.status_code == 200
    assert 'id="analyze-panel"' not in r.text


# ---------------------------------------------------------------------------
# 6. My account sub-nav
# ---------------------------------------------------------------------------

def test_my_account_subnav():
    app, store = _app_with()
    client, _account = _customer_client(app, store)
    for path, active in (("/account/dashboard", "My account"),
                         ("/account/security", "Security"),
                         ("/account/subscribe", "Subscribe")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "class='v-subnav'" in r.text, path
        for label in ("My account", "Security", "Subscribe"):
            assert label in r.text, (path, label)
        # The current page is a non-clickable span, never a link.
        assert (f"<span class='v-subnav-item active' "
                f"aria-current='page'>{active}</span>") in r.text, path


# ---------------------------------------------------------------------------
# 7. Website page section order
# ---------------------------------------------------------------------------

def test_website_page_section_order():
    app, store = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_customer(store, account)
    r = client.get(f"/account/sites/{site['id']}")
    assert r.status_code == 200
    text = r.text
    markers = ("<h2>Funnel — visitors to paying customers</h2>",
               "<h2>Run agent work</h2>", "<h2>Jobs for this site</h2>",
               "<h2>Paying customers</h2>", "<h2>Connections</h2>",
               "<h2>Data &amp; privacy controls</h2>",
               "<h2>How attribution works</h2>")
    positions = [text.index(m) for m in markers]
    assert positions == sorted(positions), markers


# ---------------------------------------------------------------------------
# 8. Approvals rows are expandable
# ---------------------------------------------------------------------------

def test_approvals_rows_expandable():
    registry = BusinessRegistry()
    approvals = ApprovalGate()
    app, store = _app_with(approvals=approvals, registry=registry)
    client, _account = _customer_client(app, store)
    _biz, appr = _seed_approval(registry, approvals, CUSTOMER_A[0])
    r = client.get("/approvals")
    assert r.status_code == 200
    assert "class='v-expandable'" in r.text
    assert "Full payload" in r.text
    assert "raise the daily cap" in r.text
    # Deciding still works from the table.
    r = client.post(f"/approvals/{appr.id}/decide",
                    data={"approved": "true"})
    assert r.status_code == 200
    assert len(approvals.pending()) == 0


# ---------------------------------------------------------------------------
# 9. Analytics is the single home for proof
# ---------------------------------------------------------------------------

def test_analytics_has_donut_and_report():
    app, store = _app_with()
    client, account = _customer_client(app, store)
    _seed_customer(store, account)
    r = client.get("/analytics")
    assert r.status_code == 200
    assert "Conversion status" in r.text
    assert "Acquisition trend" in r.text
    assert "Reports" in r.text
    assert "href='/report'" in r.text
    # /report itself is unchanged: it still has the breakdown card.
    r = client.get("/report")
    assert r.status_code == 200
    assert "Conversion breakdown" in r.text


# ---------------------------------------------------------------------------
# 10. Customers signature link + Integrations Stripe hint
# ---------------------------------------------------------------------------

def test_customers_signature_links_to_campaign():
    app, store = _app_with()
    client, account = _customer_client(app, store)
    _seed_customer(store, account)
    r = client.get("/customers")
    assert r.status_code == 200
    assert "<a href='/campaigns'>vtn_act-1</a>" in r.text


def test_integrations_stripe_hint():
    app, store = _app_with()
    client, account = _customer_client(app, store)
    _seed_customer(store, account)
    r = client.get("/integrations")
    assert r.status_code == 200
    assert ("Connect Stripe sales for ada.example.com to count paying "
            "customers") in r.text


# ---------------------------------------------------------------------------
# 11. Websites page groups
# ---------------------------------------------------------------------------

def test_websites_page_operator_businesses_group():
    registry = BusinessRegistry()
    app, store = _app_with(registry=registry)
    op = _operator_client(app, store)
    biz = registry.create("Farelune", "travel")
    registry.assign_owner(biz.id, "operator@example.com")
    r = op.get("/portfolio")
    assert r.status_code == 200
    assert "<title>Websites — Ventoryn</title>" in r.text
    assert "Your websites" in r.text
    assert "Operator businesses" in r.text
    assert "href='/businesses'" in r.text
    assert 'id="analyze-panel"' not in r.text


def test_agents_page_states_the_approval_rule():
    app, store = _app_with()
    client, _account = _customer_client(app, store)
    r = client.get("/agents")
    assert r.status_code == 200
    assert ("Agents research and draft. Nothing is published until "
            "you approve it on") in r.text
    assert "href='/approvals'" in r.text


def test_campaigns_page_intro_line():
    app, store = _app_with()
    client, account = _customer_client(app, store)
    _seed_customer(store, account)
    r = client.get("/campaigns")
    assert r.status_code == 200
    assert ("Work that's live, and what it earned. Campaigns start "
            "from a website") in r.text

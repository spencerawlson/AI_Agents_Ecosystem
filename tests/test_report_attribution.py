"""The customer Acquisition & Attribution report (reference image 3).

The customer /report is the attribution-first view: verified paying
customers, Verified / Pending / Unattributed
counts, the attribution flow with evidence checks, the conversion
status donut, campaign performance, the acquisition trend from real
data, the conversion breakdown, the data-source legend, and the
Data & Privacy controls (three separate toggles per site). Every
figure is real and tagged with its source; nothing is invented.
The OPERATOR's agent & financial report is a different page and
keeps the old shell untouched.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import ventoryn.saas as saas_module
from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from ventoryn.saas import (
    SaasConfig,
    SaasStore,
    handle_customer_sale_webhook,
)
from ventoryn.signature import sign_agent_link
from ventoryn.app import create_app
from orchestrator.approvals import ApprovalGate

from _signin import (
    OPERATOR_EMAIL,
    fresh_totp_code,
    operator_store_and_config,
    sign_in,
)

CUSTOMER_A = ("ada@example.com", "google-sub-ada", "Ada")
ADMIN_PASSWORD = "test-admin-password"


def _app_with(approvals=None):
    store, config = operator_store_and_config(
        admin_password=ADMIN_PASSWORD)
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     approvals=approvals or ApprovalGate(),
                     audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store


def _site_with_evidence(app, store, client, account, gate):
    """A site with the full evidence set: a job ran, an approval was
    decided, a signed link exists, Plausible visitors exist, a Stripe
    payment and a verified conversion exist."""
    site = store.create_site(account["id"], "https://ada.example.com")
    # A job ran (credits granted, analyze charged).
    store.grant_credits(account["id"], 100, "test allowance")
    store.charge_credits(account["id"], "analyze", site["id"])
    # An approval for this site was decided by the account.
    appr = gate.request("ad_spend_increase", amount_usd=10.0,
                        business_id=f"site:{site['id']}")
    gate.decide(appr.id, True, "ada@example.com")
    # A signed Axionory link was created. (The key is passed
    # explicitly here — in production it comes from
    # VENTORYN_TOKEN_KEY in the server environment.)
    signed, tag = sign_agent_link(
        "https://ada.example.com/welcome", site["id"],
        "ada.example.com", "creative", "job-1", store, key="test-key")
    # Plausible connected, with visitors (patched — no live API here).
    store.update_connection(site["id"], "plausible", "connected",
                            "Connected.", {"api_key": "plausible-key",
                                         "site_domain": "ada.example.com"})
    # A Stripe payment carrying the tag (verified) + a manual one
    # (pending) + a Stripe payment without a tag (unattributed).
    event = {"id": "evt_rep_1", "type": "checkout.session.completed",
             "data": {"object": {
                 "amount_total": 1900, "currency": "usd",
                 "metadata": {"ventoryn_site_id": site["id"],
                              "ventoryn_tag": tag}}}}
    assert handle_customer_sale_webhook(store, site["id"], event) == "ok"
    store.record_conversion(site["id"], 1900, "usd", "manual")
    event2 = {"id": "evt_rep_2", "type": "checkout.session.completed",
              "data": {"object": {
                  "amount_total": 2500, "currency": "usd",
                  "metadata": {"ventoryn_site_id": site["id"]}}}}
    assert handle_customer_sale_webhook(store, site["id"], event2) == "ok"
    # Stripe sales connected so the dollar figures can show.
    store.update_connection(site["id"], "stripe_sales", "connected",
                            "Connected.", {"webhook_secret": "whsec_test"})
    return site, tag


def _set_privacy(client, store, account, site_id, revenue=True,
                 count=True, details=True):
    r = client.post(
        f"/websites/sites/{site_id}/privacy",
        data={"revenue_show": "on" if revenue else "",
              "count_show": "on" if count else "",
              "details_show": "on" if details else "",
              "totp_code": fresh_totp_code(store, account["id"])},
        follow_redirects=False)
    assert r.status_code == 303, r.text


def test_report_structure_per_reference():
    gate = ApprovalGate()
    app, store = _app_with(approvals=gate)
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site, tag = _site_with_evidence(app, store, client, account, gate)
    _set_privacy(client, store, account, site["id"])
    real_visitors = saas_module.plausible_visitors
    saas_module.plausible_visitors = lambda key, domain: 42
    try:
        r = client.get("/report")
    finally:
        saas_module.plausible_visitors = real_visitors
    assert r.status_code == 200
    text = r.text
    # Title + subtitle.
    assert "Acquisition &amp; Attribution" in text
    assert "Track verified paying customers" in text
    # Progress: 1 verified customer, with the 7-day pill.
    assert "1" in text and "Verified customers" in text
    assert "Goal: 100" not in text
    assert "/ 100" not in text
    assert "verified customers (7 days)" in text
    # The revenue TOTAL is $19 + $19 + $25 = $63.00 (the per-sale
    # amounts live on the site page, not the report).
    assert "$63.00" in text
    # The three status cards.
    for label in ("Verified", "Pending", "Unattributed"):
        assert label in text, label
    # The attribution flow, all six steps with real evidence.
    assert "Attribution flow" in text
    for step in ("AI Recommendation", "Customer Approval",
                 "Signed Campaign Link", "Website Visit",
                 "Stripe Payment", "Verified Conversion"):
        assert step in text, step
    # Every flow step shows a check: six "done" steps. (The explainer
    # below the flow mentions "No evidence yet" as the label for steps
    # without evidence — that one static occurrence is fine.)
    assert text.count("v-flow-step done") == 6
    assert text.count("No evidence yet") == 1
    # The conversion-status donut + the campaign table.
    assert "Conversion status" in text
    assert "Campaign performance" in text
    assert "ada.example.com" in text  # the signed campaign's target
    assert tag in text  # the signature next to the attributed sale
    # The three privacy toggles, per site.
    assert "Show my revenue on Axionory" in text
    assert "Show my customer count" in text
    assert "Show conversion details" in text
    # The trend, the breakdown, and the data-source legend.
    assert "Acquisition trend" in text
    assert "Conversion breakdown" in text
    assert "Data source &amp; accuracy" in text
    assert "Real data" in text
    assert "Estimated data" in text
    assert "Demonstration data" in text
    # No estimated/projection line is ever drawn — the trend is real
    # data only.
    assert "does not show estimated data" in text
    # The shared sidebar, not the old pill nav.
    assert "class='app-sidebar'" in text
    assert "class='customer-nav'" not in text


def test_operator_report_hidden_revenue_does_not_crash():
    """Regression: the operator report must not 500 when a site's
    revenue is hidden (the default) — the totals read "hidden"."""
    gate = ApprovalGate()
    app, store = _app_with(approvals=gate)
    client = TestClient(app)
    account = sign_in(client, store, email="operator@example.com",
                      sub="google-sub-op", name="Op")
    site = store.create_site(account["id"], "https://ada.example.com",
                             "ca", "en")
    store.record_conversion(site["id"], 1900, "usd", "manual")
    r = client.get("/report")
    assert r.status_code == 200, r.text[:400]
    assert "Agent &amp; Financial Report" in r.text
    assert "hidden" in r.text


def test_report_empty_state_is_honest():
    app, store = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    store.create_site(account["id"], "https://ada.example.com")
    r = client.get("/report")
    assert r.status_code == 200
    assert "0" in r.text  # 0 verified customers
    assert "Verified customers" in r.text
    assert "/ 100" not in r.text
    # Every flow step says so when there is no evidence.
    assert "No evidence yet" in r.text
    assert "No signed campaigns yet" in r.text
    assert "No data" in r.text  # the empty donut
    # No invented numbers: no funnel counts, no trend line.
    assert "2,840" not in r.text
    assert "4,320" not in r.text


def test_report_respects_the_three_toggles():
    gate = ApprovalGate()
    app, store = _app_with(approvals=gate)
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site, tag = _site_with_evidence(app, store, client, account, gate)
    # Revenue off (default): no dollar figure on the report.
    r = client.get("/report")
    assert "$19.00" not in r.text
    assert "hidden" in r.text
    # Revenue on: the total appears ($19 + $19 + $25 = $63.00).
    _set_privacy(client, store, account, site["id"], revenue=True)
    r = client.get("/report")
    assert "$63.00" in r.text
    # Count off: the counts hide, the trend hides, the ring hides —
    # but the underlying verified count is untouched.
    _set_privacy(client, store, account, site["id"],
                 revenue=True, count=False)
    r = client.get("/report")
    assert "hidden" in r.text
    assert store.conversion_status_counts([site["id"]])["verified"] == 1
    # Details off: the per-sale rows hide.
    _set_privacy(client, store, account, site["id"],
                 revenue=True, count=True, details=False)
    r = client.get(f"/websites/sites/{site['id']}")
    assert "Conversion details are hidden" in r.text


def test_customer_approval_evidenced_by_a_decided_campaign():
    """Regression: approving/rejecting a campaign proposal — the
    approval action real customers actually take — never touches
    ApprovalGate (it is its own saas_campaigns.status column, see
    ventoryn/campaigns.py). "Customer Approval" must still show
    evidence from that alone, with no gate decision at all."""
    from ventoryn import campaigns as camp

    app, store = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    campaign = camp.propose(
        store, site, [{"text": "Hello", "link": site["url"]}],
        days=1, posts_per_day=1, with_link=True, source="autopilot")
    camp.schedule(store, campaign)
    r = client.get("/report")
    assert r.status_code == 200
    i = r.text.index("Customer Approval")
    step_html = r.text[max(0, i - 400):i]
    assert "v-flow-step done" in step_html


def test_operator_report_content_unchanged():
    app, store = _app_with()
    client = TestClient(app)
    sign_in(client, store)  # the operator
    r = client.get("/report")
    assert r.status_code == 200
    # The operator's Agent & Financial Report keeps its content —
    # only its shell changed (platform sidebar, no pill nav).
    assert "class='app-sidebar'" in r.text
    assert "class='customer-nav'" not in r.text
    assert "v-burger" not in r.text
    assert "Agent &amp; Financial Report" in r.text
    assert "Acquisition &amp; Attribution" not in r.text

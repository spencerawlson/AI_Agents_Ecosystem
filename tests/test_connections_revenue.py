"""Connections + revenue + attribution (Ventoryn).

- The site page has a Connections section: Reddit, X, Plausible,
  Stripe sales — each with its status and a Connect / Disconnect
  action. Reddit/X honestly say "not connected — app registration
  needed" until the operator registers the platform OAuth apps.
- Agents may only post/reply on accounts that are actually
  connected (the posting gate).
- Revenue: the customer plugs their site's Stripe sales in and
  chooses, with THREE separate settings, what Ventoryn shows —
  revenue (dollars, off by default), customer count (on by
  default), conversion details (on by default). Off hides the
  figures everywhere; the paying-customer count still increments.
- Attribution: conversions attributed to agent work appear in the
  customer's report with their signature; "unavailable" until a
  revenue source is connected; never invented.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from ventoryn.saas import (
    CONNECTION_CONSENT,
    CONNECTION_SCOPES,
    SaasConfig,
    SaasStore,
    check_posting_allowed,
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


def _app_with():
    store, config = operator_store_and_config()
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     approvals=ApprovalGate(), audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store, config


def _site_with_sale(app, store, client, account, amount=1900):
    site = store.create_site(account["id"], "https://ada.example.com")
    store.record_conversion(site["id"], amount, "usd", "manual")
    return site


def _connect_stripe(client, site_id):
    r = client.post(
        f"/account/sites/{site_id}/connections/stripe_sales",
        data={"webhook_secret": "whsec_test"}, follow_redirects=False)
    assert r.status_code == 303, r.text


def _set_privacy(client, store, account, site_id, revenue=True,
                 count=True, details=True):
    r = client.post(
        f"/account/sites/{site_id}/privacy",
        data={"revenue_show": "on" if revenue else "",
              "count_show": "on" if count else "",
              "details_show": "on" if details else "",
              "totp_code": fresh_totp_code(store, account["id"])},
        follow_redirects=False)
    assert r.status_code == 303, r.text


def test_connections_list_honest_not_connected_states():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    r = client.get(f"/account/sites/{site['id']}")
    assert r.status_code == 200
    for kind in ("Reddit", "X", "Plausible", "Stripe"):
        assert kind in r.text, kind
    # Four connections, none connected yet.
    assert r.text.count("Not connected") >= 4
    # Reddit/X: the honest app-registration limit, on the page.
    assert "app registration is needed" in r.text
    # The plain-words consent + the minimum scopes are shown.
    assert "cannot send direct messages" in r.text.lower()
    assert CONNECTION_SCOPES["reddit"] in r.text
    assert CONNECTION_SCOPES["x"] in r.text
    # X's API is paid and rate-limited — said plainly.
    assert "paid and rate-limited" in r.text
    # And the store agrees: nothing is connected.
    for conn in store.get_connections(site["id"]):
        assert conn["status"] == "not_connected", conn


def test_unconnected_account_cannot_post_gate_enforced():
    app, store, config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    for kind in ("reddit", "x", "plausible", "stripe_sales"):
        allowed, reason = check_posting_allowed(
            store, account, site["id"], kind, config)
        assert allowed is False, kind
        assert "Not connected" in reason, reason
    # The Connect action for Reddit refuses honestly (no free API
    # access exists — nothing is stored, nothing is faked).
    r = client.post(f"/account/sites/{site['id']}/connections/reddit",
                    data={})
    assert r.status_code == 400
    assert "not available" in r.text
    conn = store.get_connection(site["id"], "reddit") or {}
    assert conn.get("status") != "connected"
    # Pause-all-posting also blocks, even if a connection existed.
    store.set_posting_paused(account["id"], True)
    store.update_connection(site["id"], "plausible", "connected",
                            "Connected.", {"api_key": "plausible-key"})
    account = store.get_account(account["id"])  # re-read: the dict
    # from sign-in is a snapshot from before the pause.
    allowed, reason = check_posting_allowed(
        store, account, site["id"], "plausible", config)
    assert allowed is False
    assert "paused" in reason.lower()


def test_revenue_toggle_off_hides_on_shows():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = _site_with_sale(app, store, client, account)
    _connect_stripe(client, site["id"])
    # Off by default: no dollar figure anywhere on the report.
    r = client.get("/report")
    assert r.status_code == 200
    assert "$19.00" not in r.text
    assert "hidden" in r.text
    # On: the revenue appears in the report and the funnel.
    _set_privacy(client, store, account, site["id"], revenue=True)
    r = client.get("/report")
    assert r.status_code == 200
    assert "$19.00" in r.text
    r = client.get(f"/account/sites/{site['id']}")
    assert "$19.00" in r.text
    # Off again: hidden everywhere, again.
    _set_privacy(client, store, account, site["id"], revenue=False)
    r = client.get("/report")
    assert "$19.00" not in r.text
    assert "hidden" in r.text


def test_count_toggle_hides_count_but_count_still_increments():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = _site_with_sale(app, store, client, account)
    _connect_stripe(client, site["id"])
    # Count on (default): the paying-customer count shows in the funnel.
    _set_privacy(client, store, account, site["id"],
                 revenue=True, count=True)
    r = client.get(f"/account/sites/{site['id']}")
    assert "<td>1</td>" in r.text  # one paying customer in the funnel
    assert store.paying_customer_count(site["id"]) == 1
    # Count off: the report hides the count…
    _set_privacy(client, store, account, site["id"],
                 revenue=True, count=False)
    r = client.get(f"/account/sites/{site['id']}")
    assert "hidden — the customer count is not shown" in r.text
    # …but the count itself still increments — the sale still counts
    # toward the 100 goal.
    store.record_conversion(site["id"], 1900, "usd", "manual")
    assert store.paying_customer_count(site["id"]) == 2
    assert store.revenue_for_site(site["id"])["paying_customers"] is None


def test_details_toggle_hides_per_sale_rows():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = _site_with_sale(app, store, client, account)
    _connect_stripe(client, site["id"])
    _set_privacy(client, store, account, site["id"],
                 revenue=True, details=True)
    r = client.get(f"/account/sites/{site['id']}")
    assert "$19.00" in r.text
    _set_privacy(client, store, account, site["id"],
                 revenue=True, details=False)
    r = client.get(f"/account/sites/{site['id']}")
    assert "Conversion details are hidden" in r.text


def test_attributed_conversion_appears_in_report_with_signature():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    _connect_stripe(client, site["id"])
    # An agent link, signed and recorded. (The key is passed
    # explicitly here — in production it comes from
    # VENTORYN_TOKEN_KEY in the server environment.)
    signed, tag = sign_agent_link(
        "https://ada.example.com/welcome", site["id"],
        "ada.example.com", "creative", "job-1", store, key="test-key")
    assert "utm_source=ventoryn" in signed
    assert tag.startswith("vtn_")
    # The payment carries the tag to checkout.
    event = {"id": "evt_sig_1", "type": "checkout.session.completed",
             "data": {"object": {
                 "amount_total": 1900, "currency": "usd",
                 "metadata": {"ventoryn_site_id": site["id"],
                              "ventoryn_tag": tag}}}}
    assert handle_customer_sale_webhook(store, site["id"], event) == "ok"
    # A payment without the tag is unattributed, never guessed.
    event2 = {"id": "evt_sig_2", "type": "checkout.session.completed",
              "data": {"object": {
                  "amount_total": 2500, "currency": "usd",
                  "metadata": {"ventoryn_site_id": site["id"]}}}}
    assert handle_customer_sale_webhook(store, site["id"], event2) == "ok"
    _set_privacy(client, store, account, site["id"], revenue=True)
    r = client.get("/report")
    assert r.status_code == 200
    # The report shows the revenue TOTAL ($19 + $25 = $44.00), not
    # the per-sale amounts (those live on the site page).
    assert "$44.00" in r.text
    assert tag in r.text  # the signature next to the attributed sale
    assert "Verified" in r.text
    assert "Unattributed" in r.text
    # The site page shows the per-sale rows with their signatures.
    r = client.get(f"/account/sites/{site['id']}")
    assert "$19.00" in r.text
    assert "$25.00" in r.text
    assert tag in r.text


def test_funnel_unavailable_until_revenue_source_connected():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    r = client.get(f"/account/sites/{site['id']}")
    assert r.status_code == 200
    # No Stripe sales connection: revenue reads "unavailable", and
    # no dollar figure is invented for it.
    assert "unavailable — connect your Stripe webhook below" in r.text
    assert "$19.00" not in r.text

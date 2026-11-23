"""The signed tag reaches the merchant's Stripe payment.

Before this, a tag arrived on the merchant's site in ``utm_content``
and stopped there: nothing carried it to checkout, so every real sale
was "unattributed". Now:

- the tracker (``data-carry``) adds the tag to Stripe Payment Links as
  ``client_reference_id`` and keeps the UTM parameters on the site's
  own links — in the URL only, nothing stored on the device;
- the customer-sale webhook reads the tag from ``client_reference_id``,
  and accepts a sale with no ``ventoryn_site_id`` metadata only when
  the tag is one Axionory recorded for THIS site.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ventoryn.analytics import TRACKER_JS, snippet
from ventoryn.saas import SaasStore, handle_customer_sale_webhook
from ventoryn.signature import (parse_tag, sign_agent_link, tag_from_reference,
                                tag_to_reference)


def _store_with_site(url="https://ada.example.com"):
    store = SaasStore(":memory:")
    account = store.create_account("sub-ada", "ada@example.com", "Ada")
    site = store.create_site(account["id"], url)
    return store, site


def _signed(store, site):
    _, tag = sign_agent_link("https://ada.example.com/welcome", site["id"],
                             "ada.example.com", "creative", "job-1", store,
                             key="test-key")
    return tag


def _session(event_id, obj):
    return {"id": event_id, "type": "checkout.session.completed",
            "data": {"object": {"amount_total": 1900, "currency": "usd", **obj}}}


def test_reference_round_trip_uses_only_payment_link_safe_characters():
    store, site = _store_with_site()
    tag = _signed(store, site)
    ref = tag_to_reference(tag)
    assert "." not in ref and ref.startswith("vtn_")
    assert all(c.isalnum() or c in "-_" for c in ref)
    assert tag_from_reference(ref) == tag
    assert tag_from_reference(tag) == tag  # the tag itself is accepted too
    for junk in ("", "acct_123", "vtn_", "vtn_abc", "cus_vtn_1_2"):
        assert tag_from_reference(junk) == ""
    assert tag_to_reference("not-a-tag") == ""
    assert parse_tag(tag_from_reference(ref))


def test_payment_link_sale_with_the_tag_is_attributed():
    store, site = _store_with_site()
    tag = _signed(store, site)
    event = _session("evt_pl_1", {"client_reference_id": tag_to_reference(tag)})
    assert handle_customer_sale_webhook(store, site["id"], event) == "ok"
    [conv] = store.list_conversions(site["id"])
    assert conv["attribution"] == "ventoryn"
    assert conv["signature_tag"] == tag


def test_without_site_metadata_only_a_tag_of_this_site_counts():
    store, site = _store_with_site()
    other_store_site = store.create_site(
        store.create_account("sub-bo", "bo@example.com", "Bo")["id"],
        "https://bo.example.com")
    other_tag = _signed(store, other_store_site)
    # No metadata, no tag: not this site's sale to count.
    assert handle_customer_sale_webhook(
        store, site["id"], _session("evt_a", {})) == "ignored"
    # A tag Axionory never recorded (forged / mistyped).
    assert handle_customer_sale_webhook(
        store, site["id"],
        _session("evt_b", {"client_reference_id": "vtn_1-abc_0123456789ab"})) == "ignored"
    # Another site's real tag does not prove this site.
    assert handle_customer_sale_webhook(
        store, site["id"],
        _session("evt_c", {"client_reference_id": tag_to_reference(other_tag)})) == "ignored"
    assert store.list_conversions(site["id"]) == []


def test_site_metadata_for_another_site_still_wins_over_a_tag():
    store, site = _store_with_site()
    tag = _signed(store, site)
    event = _session("evt_d", {"client_reference_id": tag_to_reference(tag),
                               "metadata": {"ventoryn_site_id": "someone-else"}})
    assert handle_customer_sale_webhook(store, site["id"], event) == "ignored"


def test_metadata_path_unchanged():
    store, site = _store_with_site()
    tag = _signed(store, site)
    assert handle_customer_sale_webhook(store, site["id"], _session(
        "evt_e", {"metadata": {"ventoryn_site_id": site["id"],
                               "ventoryn_tag": tag}})) == "ok"
    assert handle_customer_sale_webhook(store, site["id"], _session(
        "evt_f", {"metadata": {"ventoryn_site_id": site["id"]}})) == "ok"
    rows = {c["stripe_event_id"]: c["attribution"]
            for c in store.list_conversions(site["id"])}
    assert rows == {"evt_e": "ventoryn", "evt_f": "unattributed"}


def test_tracker_carries_the_tag_without_storing_anything():
    assert "data-carry" in snippet("abc", "https://www.axionory.com")
    assert "client_reference_id" in TRACKER_JS
    assert "buy.stripe.com" in TRACKER_JS
    assert "ventoryn.tag" in TRACKER_JS
    for storage in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert storage not in TRACKER_JS

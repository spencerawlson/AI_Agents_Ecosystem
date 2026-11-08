"""The Ventoryn signature — attribution, proved, never guessed.

Every agent output that contains a link carries a signature: UTM
parameters (utm_source=ventoryn, utm_medium=<agent>,
utm_campaign=<site>) PLUS a signed per-action tag (vtn_<id>)
generated per site + per agent action and RECORDED when the link is
created (what it points to, which agent, which job, when). A payment
is attributed to Ventoryn work ONLY where the signature reaches the
payment (the tag in Stripe metadata / the webhook match); otherwise
the sale is reported as unattributed, never assigned by guesswork.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.community_listening import sign_reply_draft
from ventoryn.saas import SaasStore, handle_customer_sale_webhook
from ventoryn.signature import (
    make_tag,
    new_action_id,
    parse_tag,
    sign_agent_link,
    sign_url,
    tag_from_url,
    verify_tag,
)

KEY = "test-vent signature key"


def _event(event_id, site_id, amount=1900, tag=None):
    metadata = {"ventoryn_site_id": site_id}
    if tag:
        metadata["ventoryn_tag"] = tag
    return {"id": event_id, "type": "checkout.session.completed",
            "data": {"object": {
                "amount_total": amount, "currency": "usd",
                "metadata": metadata}}}


def test_tag_is_signed_and_bound_to_site_and_agent():
    tag = make_tag("site-1", "creative", "act-1", key=KEY)
    assert tag.startswith("vtn_act-1.")
    assert verify_tag(tag, "site-1", "creative", "act-1", key=KEY)
    # The same tag on another site, or another agent, fails.
    assert not verify_tag(tag, "site-2", "creative", "act-1", key=KEY)
    assert not verify_tag(tag, "site-1", "social", "act-1", key=KEY)
    assert not verify_tag(tag, "site-1", "creative", "act-1",
                          key="another key")
    assert parse_tag(tag)[0] == "act-1"
    assert parse_tag("not-a-tag") is None


def test_sign_url_carries_utm_plus_tag():
    tag = make_tag("site-1", "creative", "act-1", key=KEY)
    signed = sign_url("https://example.com/post?ref=home",
                      "mysite.com", "creative", tag)
    assert "utm_source=ventoryn" in signed
    assert "utm_medium=creative" in signed
    assert "utm_campaign=mysite.com" in signed
    assert f"utm_content={tag}" in signed
    assert "ref=home" in signed  # existing params are kept
    assert tag_from_url(signed) == tag
    assert tag_from_url("https://example.com/post") is None


def test_sign_agent_link_records_the_link():
    store = SaasStore(":memory:")
    account = store.create_account("google-sub-ada", "ada@example.com",
                                   "Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    signed, tag = sign_agent_link(
        "https://ada.example.com/welcome", site["id"],
        "ada.example.com", "creative", "job-9", store, key=KEY)
    assert tag.startswith("vtn_")
    assert "utm_source=ventoryn" in signed
    recorded = store.get_signature_by_tag(site["id"], tag)
    assert recorded is not None
    assert recorded["agent"] == "creative"
    assert recorded["job_id"] == "job-9"
    assert recorded["target_url"] == signed
    assert recorded["action_id"]
    # Two links from the same job get different tags.
    _signed2, tag2 = sign_agent_link(
        "https://ada.example.com/welcome", site["id"],
        "ada.example.com", "creative", "job-9", store, key=KEY)
    assert tag2 != tag


def test_reply_draft_is_signed_when_a_signer_exists():
    store = SaasStore(":memory:")
    account = store.create_account("google-sub-ada", "ada@example.com",
                                   "Ada")
    site = store.create_site(account["id"], "https://ada.example.com")

    def signer(url, agent):
        return sign_agent_link(url, site["id"], "ada.example.com",
                               agent, "job-3", store, key=KEY)

    draft = ("Great question — this guide covers it, and the rest of "
             "the help you need is on the site too, with examples: "
             "https://ada.example.com")
    signed_text, tag = sign_reply_draft(draft, "https://ada.example.com",
                                        signer=signer)
    assert tag is not None
    assert tag.startswith("vtn_")
    assert "utm_source=ventoryn" in signed_text
    assert store.get_signature_by_tag(site["id"], tag) is not None
    # Without a signer the draft is returned unchanged and the caller
    # must say so — never a fake signature.
    text, tag = sign_reply_draft(draft, "https://ada.example.com")
    assert tag is None
    assert text == draft


def test_payment_with_signature_attributed_without_it_unattributed():
    store = SaasStore(":memory:")
    account = store.create_account("google-sub-ada", "ada@example.com",
                                   "Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    signed, tag = sign_agent_link(
        "https://ada.example.com/welcome", site["id"],
        "ada.example.com", "creative", "job-7", store, key=KEY)
    # The payment carries the tag: attributed to the right action.
    assert handle_customer_sale_webhook(
        store, site["id"], _event("evt_attr_1", site["id"], tag=tag)
    ) == "ok"
    conv = store.list_conversions(site["id"])[0]
    assert conv["attribution"] == "ventoryn"
    assert conv["signature_tag"] == tag
    assert conv["action_id"]
    # A payment without the tag: unattributed, never guessed.
    assert handle_customer_sale_webhook(
        store, site["id"], _event("evt_attr_2", site["id"])
    ) == "ok"
    by_event = {c["stripe_event_id"]: c
                for c in store.list_conversions(site["id"])}
    conv = by_event["evt_attr_2"]
    assert conv["attribution"] == "unattributed"
    assert not conv["signature_tag"]
    # A payment carrying a tag Ventoryn never recorded: also
    # unattributed (a forged or foreign tag proves nothing).
    assert handle_customer_sale_webhook(
        store, site["id"],
        _event("evt_attr_3", site["id"], tag="vtn_fake.0123456789ab")
    ) == "ok"
    by_event = {c["stripe_event_id"]: c
                for c in store.list_conversions(site["id"])}
    conv = by_event["evt_attr_3"]
    assert conv["attribution"] == "unattributed"

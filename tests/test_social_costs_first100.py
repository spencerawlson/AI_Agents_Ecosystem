"""Rate card + caps math, and the Founding Customer summary (verified
customers only from real-mode Stripe events with an identity).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone

import pytest

from ventoryn.first100 import first100_summary
from ventoryn.saas import SaasConfig, SaasStore, handle_customer_sale_webhook
from ventoryn.social_costs import caps_allow, estimate_cost, rate_note

KEY = "test-token-key-phase2"


@pytest.fixture(autouse=True)
def _token_key(monkeypatch):
    monkeypatch.setenv("VENTORYN_TOKEN_KEY", KEY)


def _event(event_id, site_id, amount=2000, customer="cus_123",
           email="buyer@example.com", livemode=True, tag=None):
    metadata = {"ventoryn_site_id": site_id}
    if tag:
        metadata["ventoryn_tag"] = tag
    return {"id": event_id, "type": "checkout.session.completed",
            "livemode": livemode,
            "data": {"object": {
                "amount_total": amount, "currency": "usd",
                "customer": customer, "customer_email": email,
                "metadata": metadata}}}


def _setup_site(store):
    account = store.create_account("sub-1", "a@example.com")
    site = store.create_site(account["id"], "https://example.com")
    return account, site


# -- rate card ---------------------------------------------------------------

def test_estimate_cost_x_link_post_is_20_cents():
    assert estimate_cost("x", has_link=True) == 0.20
    assert "REPORTED" in rate_note("x")
    assert "not directly verified" in rate_note("x")


def test_estimate_cost_meta_is_zero_with_honesty_note():
    assert estimate_cost("facebook", has_link=True) == 0.0
    assert estimate_cost("instagram", has_link=True) == 0.0
    assert "do not quote $0" in rate_note("facebook")
    assert "do not quote $0" in rate_note("instagram")


def test_estimate_cost_no_link_is_zero_everywhere():
    assert estimate_cost("x", has_link=False) == 0.0


def test_estimate_cost_unknown_provider_raises():
    with pytest.raises(ValueError):
        estimate_cost("tiktok")


# -- caps --------------------------------------------------------------------

def test_caps_allow_under_all_caps():
    store = SaasStore()
    account, site = _setup_site(store)
    config = SaasConfig.from_env({})
    now = datetime.now(timezone.utc)
    allowed, reason = caps_allow(store, account["id"], site["id"], 0.20,
                                 config=config, now=now)
    assert allowed
    assert "Within" in reason


def test_caps_daily_global_cap_blocks():
    store = SaasStore()
    account, site = _setup_site(store)
    config = SaasConfig.from_env({})
    now = datetime.now(timezone.utc)
    store.record_social_spend(account["id"], site["id"], "x", "d-1", 9.90)
    allowed, reason = caps_allow(store, account["id"], site["id"], 0.20,
                                 config=config, now=now)
    assert not allowed
    assert "daily limit" in reason


def test_caps_account_monthly_cap_blocks():
    store = SaasStore()
    account, site = _setup_site(store)
    other_account = store.create_account("sub-2", "b@example.com")
    other_site = store.create_site(other_account["id"],
                                   "https://other.example.com")
    # Daily cap raised out of the way so the monthly cap is the one
    # that trips.
    config = SaasConfig.from_env(
        {"VENTORYN_SOCIAL_DAILY_CAP_USD": "1000"})
    now = datetime.now(timezone.utc)
    # Spend on the OTHER site of the same account counts toward the
    # account cap but not this site's cap.
    store.record_social_spend(account["id"], site["id"], "x", "d-1", 49.90)
    allowed, reason = caps_allow(store, account["id"], site["id"], 0.20,
                                 config=config, now=now)
    assert not allowed
    assert "account" in reason and "monthly limit" in reason
    # The other account is unaffected.
    allowed2, _ = caps_allow(store, other_account["id"], other_site["id"],
                             0.20, config=config, now=now)
    assert allowed2


def test_caps_site_monthly_cap_blocks():
    store = SaasStore()
    account, site = _setup_site(store)
    config = SaasConfig.from_env(
        {"VENTORYN_SOCIAL_DAILY_CAP_USD": "1000"})
    now = datetime.now(timezone.utc)
    store.record_social_spend(account["id"], site["id"], "x", "d-1", 24.90)
    allowed, reason = caps_allow(store, account["id"], site["id"], 0.20,
                                 config=config, now=now)
    assert not allowed
    assert "site" in reason and "monthly limit" in reason


def test_social_spend_in_period_includes_x_post_log_spend():
    # The caps must see real X spend, not just saas_social_spend rows:
    # X post cost is recorded in whole cents in saas_x_post_log.
    store = SaasStore()
    account, site = _setup_site(store)
    store.record_x_post_cost(site["id"], "draft-x-1", 20)
    since = "2000-01-01T00:00:00+00:00"
    assert store.social_spend_in_period(since) == pytest.approx(0.20)
    assert store.social_spend_in_period(
        since, site_id=site["id"]) == pytest.approx(0.20)
    assert store.social_spend_in_period(
        since, account_id=account["id"]) == pytest.approx(0.20)
    # ...and the daily cap trips on it: $0.20 already spent + $9.90
    # more would take today past the $10.00 daily limit.
    config = SaasConfig.from_env({})
    now = datetime.now(timezone.utc)
    allowed, reason = caps_allow(store, account["id"], site["id"], 9.90,
                                 config=config, now=now)
    assert not allowed
    assert "daily limit" in reason


# -- founding customer -------------------------------------------------------

def test_first100_empty_is_zero_of_100():
    store = SaasStore()
    account, site = _setup_site(store)
    summary = first100_summary(store, site["id"])
    assert summary == {"goal": 100, "verified": 0, "paying": 0,
                       "remaining": 100, "attributed": 0,
                       "unattributed": 0, "recent": []}


def test_first100_counts_verified_only_after_real_mode_webhook():
    store = SaasStore()
    account, site = _setup_site(store)
    assert handle_customer_sale_webhook(
        store, site["id"], _event("evt-1", site["id"])) == "ok"
    summary = first100_summary(store, site["id"])
    assert summary["verified"] == 1
    assert summary["paying"] == 1
    assert summary["remaining"] == 99
    assert summary["unattributed"] == 1
    assert len(summary["recent"]) == 1


def test_first100_test_mode_event_recorded_but_excluded():
    # Main's semantics (unlike the slice): a test-mode webhook event
    # IS recorded (livemode=0) but is excluded from every real count.
    store = SaasStore()
    account, site = _setup_site(store)
    assert handle_customer_sale_webhook(
        store, site["id"],
        _event("evt-test", site["id"], livemode=False)) == "ok"
    summary = first100_summary(store, site["id"])
    assert summary["verified"] == 0
    assert summary["paying"] == 0
    assert summary["remaining"] == 100
    assert summary["recent"] == []
    # The row really was recorded: redelivering the same event id is
    # a duplicate, not a fresh recording.
    assert handle_customer_sale_webhook(
        store, site["id"],
        _event("evt-test", site["id"], livemode=False)) == "duplicate"


def test_first100_manual_fallback_never_counts_as_verified():
    store = SaasStore()
    account, site = _setup_site(store)
    store.record_conversion(site["id"], 2000, "usd", "manual")
    summary = first100_summary(store, site["id"])
    assert summary["paying"] == 1
    assert summary["verified"] == 0
    assert summary["remaining"] == 100
    assert summary["attributed"] == 0
    assert summary["unattributed"] == 0


def test_first100_same_stripe_customer_twice_counts_once():
    store = SaasStore()
    account, site = _setup_site(store)
    assert handle_customer_sale_webhook(
        store, site["id"], _event("evt-1", site["id"])) == "ok"
    assert handle_customer_sale_webhook(
        store, site["id"],
        _event("evt-2", site["id"], customer="cus_123")) == "ok"
    summary = first100_summary(store, site["id"])
    assert summary["paying"] == 2
    assert summary["verified"] == 1
    assert summary["remaining"] == 99
    # A second, different customer counts.
    assert handle_customer_sale_webhook(
        store, site["id"],
        _event("evt-3", site["id"], customer="cus_456",
               email="second@example.com")) == "ok"
    summary = first100_summary(store, site["id"])
    assert summary["verified"] == 2
    assert summary["remaining"] == 98


def test_first100_attributed_when_signature_reaches_payment():
    store = SaasStore()
    account, site = _setup_site(store)
    from ventoryn.signature import sign_agent_link

    signed, tag = sign_agent_link(
        "https://example.com/p", site["id"], site["host"], "social",
        "draft-1", store, key=KEY)
    assert handle_customer_sale_webhook(
        store, site["id"], _event("evt-1", site["id"], tag=tag)) == "ok"
    summary = first100_summary(store, site["id"])
    assert summary["verified"] == 1
    assert summary["attributed"] == 1
    assert summary["unattributed"] == 0


def test_first100_module_docstring_uses_founding_customer_branding():
    # Nov 9 rebrand: "First 100" branding is gone from customer-facing
    # areas, replaced by the Founding Customer Program. The module
    # name and the goal number (100) stay; only the words go.
    import ventoryn.first100 as first100_mod

    assert "Founding Customer" in first100_mod.__doc__
    assert "First 100" not in first100_mod.__doc__
    assert "first-100" not in first100_mod.__doc__

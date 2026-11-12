"""Axionory Phase 3: wallet reservations, top-ups, refunds/disputes,
auto-topup config, and the low-balance alert."""

import hashlib
import hmac
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

import ventoryn.saas as saas
from ventoryn.alerts import LOW_WALLET_BALANCE_CENTS, account_alerts
from ventoryn.app import create_app
from ventoryn.saas import InsufficientWalletFunds, SaasConfig, SaasError, SaasStore

from test_saas_shell import _account, client, config, store  # noqa: F401


# -- reservations --------------------------------------------------------

def test_reserve_settle_releases_unused_portion(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 1000, "top-up")
    resv = store.reserve_wallet_funds(account["id"], 400, "estimate")
    assert store.wallet_available_cents(account["id"]) == 600
    store.settle_wallet_reservation(resv["id"], 250, "actual cost")
    assert store.wallet_balance_cents(account["id"]) == 750
    assert store.wallet_reserved_cents(account["id"]) == 0
    assert store.wallet_available_cents(account["id"]) == 750


def test_reserve_refused_over_available(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 100, "top-up")
    with pytest.raises(InsufficientWalletFunds):
        store.reserve_wallet_funds(account["id"], 200, "estimate")
    assert store.wallet_reserved_cents(account["id"]) == 0


def test_release_charges_nothing(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 500, "top-up")
    resv = store.reserve_wallet_funds(account["id"], 300, "estimate")
    store.release_wallet_reservation(resv["id"])
    assert store.wallet_balance_cents(account["id"]) == 500
    assert store.wallet_reserved_cents(account["id"]) == 0


def test_settle_is_idempotent(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 500, "top-up")
    resv = store.reserve_wallet_funds(account["id"], 300, "estimate")
    store.settle_wallet_reservation(resv["id"], 200, "actual")
    store.settle_wallet_reservation(resv["id"], 999, "retried settle")
    assert store.wallet_balance_cents(account["id"]) == 300  # not double-charged


def test_release_is_idempotent(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 500, "top-up")
    resv = store.reserve_wallet_funds(account["id"], 300, "estimate")
    store.release_wallet_reservation(resv["id"])
    store.release_wallet_reservation(resv["id"])
    assert store.wallet_reserved_cents(account["id"]) == 0


def test_concurrent_reservations_cannot_overspend_the_wallet(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 1000, "top-up")
    results = []

    def try_reserve():
        try:
            store.reserve_wallet_funds(account["id"], 300, "race")
            results.append(True)
        except InsufficientWalletFunds:
            results.append(False)

    threads = [threading.Thread(target=try_reserve) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # $1000 / $300 = 3 can succeed, not 4 or more.
    assert results.count(True) == 3
    assert store.wallet_reserved_cents(account["id"]) == 900


# -- top-up checkout -------------------------------------------------------

def test_topup_session_builds_price_data_and_metadata(config, monkeypatch):
    captured = {}

    def fake_post(url, fields, headers=None, urlopen=None):
        captured.update(fields)
        return {"id": "cs_1", "url": "https://checkout.stripe.com/pay/cs_1"}

    monkeypatch.setattr(saas, "_http_post_form", fake_post)
    account = {"id": "acct_1", "email": "a@example.com"}
    result = saas.create_wallet_topup_session(config, account, 2500)
    assert result["id"] == "cs_1"
    assert captured["mode"] == "payment"
    assert captured["line_items[0][price_data][unit_amount]"] == "2500"
    assert captured["metadata[kind]"] == "wallet_topup"
    assert captured["metadata[account_id]"] == "acct_1"


def test_topup_amount_out_of_bounds_raises(config):
    account = {"id": "acct_1", "email": "a@example.com"}
    with pytest.raises(SaasError):
        saas.create_wallet_topup_session(config, account, 1)  # below min
    with pytest.raises(SaasError):
        saas.create_wallet_topup_session(config, account, 10**9)  # above max


# -- webhook: top-up credit + refund/dispute reversal ------------------------

def _client(config):
    store = SaasStore(":memory:")
    return TestClient(create_app(saas_store=store, saas_config=config)), store


def _signed(secret, event, timestamp=None):
    payload = json.dumps(event).encode("utf-8")
    timestamp = timestamp if timestamp is not None else int(time.time())
    sig = hmac.new(secret.encode("utf-8"),
                   f"{timestamp}.".encode("utf-8") + payload,
                   hashlib.sha256).hexdigest()
    return payload, {"stripe-signature": f"t={timestamp},v1={sig}"}


def test_webhook_topup_credits_wallet_once_and_skips_subscription_fields(config):
    client, store = _client(config)
    account = _account(store)
    event = {"id": "evt_topup_1", "type": "checkout.session.completed",
             "data": {"object": {"client_reference_id": account["id"],
                                 "amount_total": 2500,
                                 "payment_intent": "pi_1",
                                 "metadata": {"kind": "wallet_topup",
                                              "account_id": account["id"]}}}}
    payload, headers = _signed("whsec_platform", event)
    r = client.post("/account/billing/webhook", content=payload, headers=headers)
    assert r.status_code == 200 and r.json()["result"] == "ok"
    account = store.get_account(account["id"])
    assert store.wallet_balance_cents(account["id"]) == 2500
    # Never touched: no subscription was involved in this checkout.
    assert account["subscription_status"] == "none"
    assert account["plan"] == ""
    r2 = client.post("/account/billing/webhook", content=payload, headers=headers)
    assert r2.json()["result"] == "duplicate"
    assert store.wallet_balance_cents(account["id"]) == 2500


def test_webhook_refund_reverses_the_topup(config):
    client, store = _client(config)
    account = _account(store)
    topup_event = {"id": "evt_topup_2", "type": "checkout.session.completed",
                   "data": {"object": {"client_reference_id": account["id"],
                                       "amount_total": 5000,
                                       "payment_intent": "pi_2",
                                       "metadata": {"kind": "wallet_topup"}}}}
    payload, headers = _signed("whsec_platform", topup_event)
    client.post("/account/billing/webhook", content=payload, headers=headers)
    assert store.wallet_balance_cents(account["id"]) == 5000

    refund_event = {"id": "evt_refund_1", "type": "charge.refunded",
                    "data": {"object": {"payment_intent": "pi_2"}}}
    payload, headers = _signed("whsec_platform", refund_event)
    r = client.post("/account/billing/webhook", content=payload, headers=headers)
    assert r.json()["result"] == "ok"
    assert store.wallet_balance_cents(account["id"]) == 0


def test_webhook_dispute_closed_won_does_not_reverse(config):
    client, store = _client(config)
    account = _account(store)
    topup_event = {"id": "evt_topup_3", "type": "checkout.session.completed",
                   "data": {"object": {"client_reference_id": account["id"],
                                       "amount_total": 3000,
                                       "payment_intent": "pi_3",
                                       "metadata": {"kind": "wallet_topup"}}}}
    payload, headers = _signed("whsec_platform", topup_event)
    client.post("/account/billing/webhook", content=payload, headers=headers)

    won_event = {"id": "evt_dispute_1", "type": "charge.dispute.closed",
                "data": {"object": {"payment_intent": "pi_3", "status": "won"}}}
    payload, headers = _signed("whsec_platform", won_event)
    r = client.post("/account/billing/webhook", content=payload, headers=headers)
    assert r.json()["result"] == "ok"
    assert store.wallet_balance_cents(account["id"]) == 3000  # unchanged


def test_webhook_reversal_with_no_matching_topup_is_ignored(config):
    client, store = _client(config)
    event = {"id": "evt_refund_x", "type": "charge.refunded",
             "data": {"object": {"payment_intent": "pi_unknown"}}}
    payload, headers = _signed("whsec_platform", event)
    r = client.post("/account/billing/webhook", content=payload, headers=headers)
    assert r.json()["result"] == "ignored"


# -- route ----------------------------------------------------------------

def test_wallet_topup_route_redirects_to_checkout(client, store, monkeypatch):
    from test_saas_shell import _login
    import ventoryn.saas_routes as saas_routes

    _login(client, store)

    def fake_session(config, account, amount_cents):
        assert amount_cents == 1000
        return {"id": "cs_1", "url": "https://checkout.stripe.com/pay/cs_1"}

    monkeypatch.setattr(saas_routes, "create_wallet_topup_session", fake_session)
    r = client.post("/account/wallet/topup", data={"amount_cents": "1000"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.stripe.com/pay/cs_1"


def test_wallet_topup_route_rejects_out_of_bounds(client, store):
    from test_saas_shell import _login

    _login(client, store)
    r = client.post("/account/wallet/topup", data={"amount_cents": "1"},
                    follow_redirects=False)
    assert r.status_code == 400


# -- auto-topup config -----------------------------------------------------

def test_auto_topup_disabled_by_default(store):
    account = _account(store)
    assert store.needs_auto_topup(account["id"]) is False


def test_auto_topup_triggers_below_threshold(store):
    account = _account(store)
    store.set_auto_topup(account["id"], enabled=True, threshold_cents=1000,
                         amount_cents=2000, monthly_cap_cents=10000)
    assert store.needs_auto_topup(account["id"]) is True  # balance 0 <= 1000
    store.grant_wallet_credit(account["id"], 5000, "top-up")
    assert store.needs_auto_topup(account["id"]) is False


def test_auto_topup_monthly_cap_enforced_and_rolls_over(store):
    import datetime as dt

    account = _account(store)
    store.set_auto_topup(account["id"], enabled=True, threshold_cents=0,
                         amount_cents=2000, monthly_cap_cents=3000)
    now = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)
    assert store.record_auto_topup_spend(account["id"], 2000, now) is True
    assert store.record_auto_topup_spend(account["id"], 2000, now) is False  # would exceed cap
    next_month = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    assert store.record_auto_topup_spend(account["id"], 2000, next_month) is True


# -- low-balance alert ------------------------------------------------------

def test_low_wallet_balance_alert_only_for_tiered_accounts(store):
    account = _account(store)
    alerts = account_alerts(store, account)
    assert not any(a["key"].startswith("wallet_low:") for a in alerts)
    store.set_plan(account["id"], "starter")
    account = store.get_account(account["id"])
    alerts = account_alerts(store, account)
    assert any(a["key"].startswith("wallet_low:") for a in alerts)


def test_low_wallet_balance_alert_clears_once_topped_up(store):
    account = _account(store)
    store.set_plan(account["id"], "starter")
    store.grant_wallet_credit(account["id"], LOW_WALLET_BALANCE_CENTS + 100,
                              "top-up")
    account = store.get_account(account["id"])
    alerts = account_alerts(store, account)
    assert not any(a["key"].startswith("wallet_low:") for a in alerts)


# -- transactions / statement ----------------------------------------------

def test_wallet_statement_csv_is_chronological(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 1000, "first")
    store.grant_wallet_credit(account["id"], -200, "second")
    csv_text = store.wallet_statement_csv(account["id"])
    lines = csv_text.strip().splitlines()
    assert lines[0] == "date,amount_usd,reason"
    assert "first" in lines[1] and "10.00" in lines[1]
    assert "second" in lines[2] and "-2.00" in lines[2]

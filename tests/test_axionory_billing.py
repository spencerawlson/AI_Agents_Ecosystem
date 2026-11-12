"""Axionory Phase 2: subscription tiers, workspaces, wallet, and the
webhook extension — all alongside the legacy $20/month plan, which
must stay completely unaffected."""

import hashlib
import hmac
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

import ventoryn.saas as saas
from ventoryn.app import create_app
from ventoryn.entitlements import can_create_workspace
from ventoryn.plans import PLANS, STARTER_WALLET_CREDIT_CENTS, limit, plan_limits
from ventoryn.saas import SaasConfig, SaasError, SaasStore

from test_saas_shell import _account, config, store  # noqa: F401


# -- plans ---------------------------------------------------------------

def test_plan_limits_for_each_tier():
    for plan in ("starter", "growth", "pro"):
        assert plan_limits(plan) is not None
        assert plan_limits(plan)["label"] == PLANS[plan]["label"]


def test_plan_limits_none_for_legacy_and_unknown():
    assert plan_limits("") is None
    assert plan_limits("nonexistent") is None
    assert limit("", "workspaces") is None


def test_tier_workspace_counts_match_spec():
    assert limit("starter", "workspaces") == 1
    assert limit("growth", "workspaces") == 3
    assert limit("pro", "workspaces") == 10


# -- workspaces ------------------------------------------------------------

def test_ensure_default_workspace_created_lazily_and_reused(store):
    account = _account(store)
    assert store.list_workspaces(account["id"]) == []
    first = store.ensure_default_workspace(account["id"])
    assert first["name"] == "Default"
    again = store.ensure_default_workspace(account["id"])
    assert again["id"] == first["id"]
    assert len(store.list_workspaces(account["id"])) == 1


def test_create_site_assigns_the_account_default_workspace(store):
    account = _account(store)
    site = store.create_site(account["id"], "https://shop.example.com")
    workspace = store.list_workspaces(account["id"])[0]
    assert site["workspace_id"] == workspace["id"]
    # A second site reuses the same workspace, not a new one.
    site2 = store.create_site(account["id"], "https://blog.example.org")
    assert site2["workspace_id"] == workspace["id"]
    assert len(store.list_workspaces(account["id"])) == 1


# -- entitlements ------------------------------------------------------------

def test_legacy_account_is_never_workspace_limited(store):
    account = _account(store)
    assert account["plan"] == ""
    for _ in range(5):
        store.create_workspace(account["id"], "Extra")
    ok, message = can_create_workspace(store, account)
    assert ok and message == ""


def test_starter_plan_blocks_a_second_workspace(store):
    account = _account(store)
    store.set_plan(account["id"], "starter")
    account = store.get_account(account["id"])
    store.ensure_default_workspace(account["id"])
    ok, _ = can_create_workspace(store, account)
    assert not ok


def test_growth_plan_allows_up_to_three_workspaces(store):
    account = _account(store)
    store.set_plan(account["id"], "growth")
    account = store.get_account(account["id"])
    for _ in range(3):
        ok, _ = can_create_workspace(store, account)
        assert ok
        store.create_workspace(account["id"], "W")
    ok, message = can_create_workspace(store, account)
    assert not ok and "Growth plan allows 3" in message


# -- wallet ------------------------------------------------------------------

def test_grant_wallet_credit_updates_balance_and_ledger(store):
    account = _account(store)
    assert store.wallet_balance_cents(account["id"]) == 0
    new_balance = store.grant_wallet_credit(account["id"], 500, "top-up")
    assert new_balance == 500
    assert store.wallet_balance_cents(account["id"]) == 500


def test_starter_credit_granted_exactly_once(store):
    account = _account(store)
    first = store.grant_starter_wallet_credit_once(
        account["id"], STARTER_WALLET_CREDIT_CENTS)
    assert first is True
    assert store.wallet_balance_cents(account["id"]) == STARTER_WALLET_CREDIT_CENTS
    second = store.grant_starter_wallet_credit_once(
        account["id"], STARTER_WALLET_CREDIT_CENTS)
    assert second is False
    assert store.wallet_balance_cents(account["id"]) == STARTER_WALLET_CREDIT_CENTS


# -- checkout session ----------------------------------------------------

def _tier_config(config):
    return replace(config,
                   stripe_price_id_starter="price_starter",
                   stripe_price_id_growth="price_growth",
                   stripe_price_id_pro="price_pro")


def test_checkout_session_for_a_tier_uses_its_price_and_tags_the_plan(
        config, monkeypatch):
    cfg = _tier_config(config)
    captured = {}

    def fake_post(url, fields, headers=None, urlopen=None):
        captured.update(fields)
        return {"id": "cs_1", "url": "https://checkout.stripe.com/pay/cs_1"}

    monkeypatch.setattr(saas, "_http_post_form", fake_post)
    account = {"id": "acct_1", "email": "a@example.com"}
    result = saas.create_checkout_session(cfg, account, plan="growth")
    assert result["id"] == "cs_1"
    assert captured["line_items[0][price]"] == "price_growth"
    assert captured["metadata[plan]"] == "growth"


def test_checkout_session_legacy_plan_unaffected_by_tier_support(
        config, monkeypatch):
    cfg = _tier_config(config)
    captured = {}

    def fake_post(url, fields, headers=None, urlopen=None):
        captured.update(fields)
        return {"id": "cs_1", "url": "https://checkout.stripe.com/pay/cs_1"}

    monkeypatch.setattr(saas, "_http_post_form", fake_post)
    account = {"id": "acct_1", "email": "a@example.com"}
    saas.create_checkout_session(cfg, account)
    assert captured["line_items[0][price]"] == "price_20usd"
    assert "metadata[plan]" not in captured


def test_checkout_session_unconfigured_tier_raises(config):
    account = {"id": "acct_1", "email": "a@example.com"}
    with pytest.raises(SaasError):
        saas.create_checkout_session(config, account, plan="pro")


# -- webhook ------------------------------------------------------------------

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


def test_webhook_with_plan_metadata_sets_tier_and_grants_starter_credit(
        config):
    client, store = _client(config)
    account = _account(store)
    event = {"id": "evt_tier_1", "type": "checkout.session.completed",
             "data": {"object": {"client_reference_id": account["id"],
                                 "customer": "cus_1", "subscription": "sub_1",
                                 "metadata": {"plan": "growth"}}}}
    payload, headers = _signed("whsec_platform", event)
    r = client.post("/account/billing/webhook", content=payload,
                    headers=headers)
    assert r.status_code == 200 and r.json()["result"] == "ok"
    account = store.get_account(account["id"])
    assert account["plan"] == "growth"
    assert account["subscription_status"] == "active"
    # Zero included credits on a new tier — not the legacy allowance.
    assert store.credit_balance(account["id"]) == 0
    assert store.wallet_balance_cents(account["id"]) == STARTER_WALLET_CREDIT_CENTS


def test_webhook_without_plan_metadata_is_the_unchanged_legacy_path(config):
    client, store = _client(config)
    account = _account(store)
    event = {"id": "evt_legacy_1", "type": "checkout.session.completed",
             "data": {"object": {"client_reference_id": account["id"],
                                 "customer": "cus_1", "subscription": "sub_1"}}}
    payload, headers = _signed("whsec_platform", event)
    r = client.post("/account/billing/webhook", content=payload,
                    headers=headers)
    assert r.status_code == 200 and r.json()["result"] == "ok"
    account = store.get_account(account["id"])
    assert account["plan"] == ""
    assert store.credit_balance(account["id"]) == config.monthly_credits
    assert store.wallet_balance_cents(account["id"]) == 0

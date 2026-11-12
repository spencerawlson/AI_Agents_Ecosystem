"""Axionory Phase 5: the pricing engine — markup pricing, settling a
job's wallet reservation against its real (priced) usage, and the
wiring through autopilot.execute()/expire_stale_jobs()."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ventoryn.autopilot as autopilot
import ventoryn.pricing as pricing
from ventoryn.saas import CREDIT_COSTS, InsufficientWalletFunds
from ventoryn.usage_metering import persist_usage_events, record_llm_usage

from test_saas_shell import _account, store  # noqa: F401

NOW = datetime.now(timezone.utc)


# -- price_cents --------------------------------------------------------------

def test_price_cents_applies_markup_and_rounds_up():
    assert pricing.price_cents(0) == 0
    assert pricing.price_cents(10) == 15
    assert pricing.price_cents(1) == 2
    assert pricing.price_cents(3) == 5


# -- price_and_settle_job ------------------------------------------------------

def test_price_and_settle_job_prices_unpriced_events_and_settles_the_real_total(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 1000, "test")
    reservation = store.reserve_wallet_funds(account["id"], 50, "analyze job")
    persist_usage_events(
        store, [{"model": "m", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.10}],
        account_id=account["id"], job_id="job_1")
    total = pricing.price_and_settle_job(store, "job_1", reservation["id"])
    assert total == pricing.price_cents(10)  # 15c
    assert store.wallet_balance_cents(account["id"]) == 1000 - total
    assert store.wallet_reserved_cents(account["id"]) == 0  # unused part of the 50c hold freed
    rows = store.usage_events_for_job("job_1")
    assert rows[0]["customer_charge_cents"] == total
    assert rows[0]["pricing_version"] == pricing.PRICING_VERSION


def test_price_and_settle_job_can_exceed_the_reservation_estimate(store):
    """Documented tradeoff: settle charges the real priced total even
    when it runs over the estimate that was reserved — no clamp."""
    account = _account(store)
    store.grant_wallet_credit(account["id"], 1000, "test")
    reservation = store.reserve_wallet_funds(account["id"], 50, "analyze job")
    persist_usage_events(
        store, [{"model": "m", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.60}],
        account_id=account["id"], job_id="job_over")
    total = pricing.price_and_settle_job(store, "job_over", reservation["id"])
    assert total == pricing.price_cents(60) and total > 50
    assert store.wallet_balance_cents(account["id"]) == 1000 - total
    assert store.wallet_reserved_cents(account["id"]) == 0


def test_price_and_settle_job_releases_the_reservation_when_nothing_was_recorded(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 1000, "test")
    reservation = store.reserve_wallet_funds(account["id"], 50, "analyze job")
    total = pricing.price_and_settle_job(store, "job_empty", reservation["id"])
    assert total == 0
    assert store.wallet_balance_cents(account["id"]) == 1000
    assert store.wallet_reserved_cents(account["id"]) == 0


def test_price_and_settle_job_is_idempotent(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 1000, "test")
    reservation = store.reserve_wallet_funds(account["id"], 50, "analyze job")
    persist_usage_events(
        store, [{"model": "m", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.10}],
        account_id=account["id"], job_id="job_retry")
    first = pricing.price_and_settle_job(store, "job_retry", reservation["id"])
    second = pricing.price_and_settle_job(store, "job_retry", reservation["id"])
    assert first == second
    assert store.wallet_balance_cents(account["id"]) == 1000 - first  # not double-charged


# -- wiring: autopilot.execute() -----------------------------------------------

def test_autopilot_execute_reserves_and_settles_for_a_wallet_account(store, monkeypatch):
    from ventoryn.saas import SaasConfig

    config = SaasConfig(stripe_price_id="p", monthly_credits=100)
    account = _account(store)
    store.set_plan(account["id"], "starter")
    account = store.get_account(account["id"])
    store.grant_wallet_credit(account["id"], 1000, "test")
    site = store.create_site(account["id"], "https://shop.example.com")

    def fake_run(job_type, site, inputs, *, orchestrator, reports_dir, store=None):
        record_llm_usage({"model": "gpt-x", "input_tokens": 5,
                          "output_tokens": 5, "cost_usd": 0.10})
        return "done", "report.md"

    monkeypatch.setattr(autopilot.site_jobs, "run", fake_run)
    job = autopilot.execute(store, config, account, site, "analyze",
                            orchestrator=None, reports_dir=Path("."))
    assert job["status"] == "done"
    assert job["credits_charged"] == 0
    assert job["wallet_reservation_id"]
    charge = pricing.price_cents(10)
    assert store.wallet_balance_cents(account["id"]) == 1000 - charge
    assert store.wallet_reserved_cents(account["id"]) == 0
    rows = store.usage_events_for_job(job["id"])
    assert rows[0]["customer_charge_cents"] == charge


def test_autopilot_execute_settles_even_when_the_job_fails(store, monkeypatch):
    from ventoryn.saas import SaasConfig

    config = SaasConfig(stripe_price_id="p", monthly_credits=100)
    account = _account(store)
    store.set_plan(account["id"], "starter")
    account = store.get_account(account["id"])
    store.grant_wallet_credit(account["id"], 1000, "test")
    site = store.create_site(account["id"], "https://shop.example.com")

    def fake_run(job_type, site, inputs, *, orchestrator, reports_dir, store=None):
        record_llm_usage({"model": "gpt-x", "input_tokens": 5,
                          "output_tokens": 5, "cost_usd": 0.10})
        raise RuntimeError("provider call succeeded but the job failed after")

    monkeypatch.setattr(autopilot.site_jobs, "run", fake_run)
    job = autopilot.execute(store, config, account, site, "analyze",
                            orchestrator=None, reports_dir=Path("."))
    assert job["status"] == "failed"
    charge = pricing.price_cents(10)
    assert store.wallet_balance_cents(account["id"]) == 1000 - charge  # real cost not lost
    assert store.wallet_reserved_cents(account["id"]) == 0


def test_autopilot_execute_legacy_account_never_touches_the_wallet(store, monkeypatch):
    from ventoryn.saas import SaasConfig

    config = SaasConfig(stripe_price_id="p", monthly_credits=100)
    account = _account(store)  # plan == "" by default
    store.grant_credits(account["id"], 100, "test")
    site = store.create_site(account["id"], "https://shop.example.com")

    def fake_run(job_type, site, inputs, *, orchestrator, reports_dir, store=None):
        record_llm_usage({"model": "gpt-x", "input_tokens": 5,
                          "output_tokens": 5, "cost_usd": 0.10})
        return "done", "report.md"

    monkeypatch.setattr(autopilot.site_jobs, "run", fake_run)
    job = autopilot.execute(store, config, account, site, "analyze",
                            orchestrator=None, reports_dir=Path("."))
    assert job["wallet_reservation_id"] == ""
    assert job["credits_charged"] == CREDIT_COSTS["analyze"]
    assert store.wallet_balance_cents(account["id"]) == 0
    assert store.wallet_reserved_cents(account["id"]) == 0
    rows = store.usage_events_for_job(job["id"])
    assert rows[0]["customer_charge_cents"] is None  # Phase 5 never prices a legacy job


def test_execute_raises_insufficient_wallet_funds_before_running_anything(store, monkeypatch):
    from ventoryn.saas import SaasConfig

    config = SaasConfig(stripe_price_id="p", monthly_credits=100)
    account = _account(store)
    store.set_plan(account["id"], "starter")  # no wallet credit granted
    account = store.get_account(account["id"])
    site = store.create_site(account["id"], "https://shop.example.com")

    ran = {"called": False}

    def fake_run(*a, **k):
        ran["called"] = True
        return "done", "report.md"

    monkeypatch.setattr(autopilot.site_jobs, "run", fake_run)
    with pytest.raises(InsufficientWalletFunds):
        autopilot.execute(store, config, account, site, "analyze",
                          orchestrator=None, reports_dir=Path("."))
    assert not ran["called"]
    assert store.wallet_reserved_cents(account["id"]) == 0


# -- wiring: expire_stale_jobs() -----------------------------------------------

def test_expire_stale_jobs_settles_an_orphaned_wallet_reservation(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 1000, "test")
    reservation = store.reserve_wallet_funds(account["id"], 50, "analyze job")
    job = store.create_wallet_job(account["id"], "analyze", "site_1", reservation["id"])
    persist_usage_events(
        store, [{"model": "m", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.10}],
        account_id=account["id"], job_id=job["id"])
    n = autopilot.expire_stale_jobs(store, NOW + timedelta(hours=3))
    assert n == 1
    charge = pricing.price_cents(10)
    assert store.wallet_balance_cents(account["id"]) == 1000 - charge
    assert store.wallet_reserved_cents(account["id"]) == 0
    assert store.get_job(job["id"])["status"] == "failed"


def test_expire_stale_jobs_releases_an_orphaned_reservation_with_no_usage(store):
    account = _account(store)
    store.grant_wallet_credit(account["id"], 1000, "test")
    reservation = store.reserve_wallet_funds(account["id"], 50, "analyze job")
    store.create_wallet_job(account["id"], "analyze", "site_1", reservation["id"])
    n = autopilot.expire_stale_jobs(store, NOW + timedelta(hours=3))
    assert n == 1
    assert store.wallet_balance_cents(account["id"]) == 1000
    assert store.wallet_reserved_cents(account["id"]) == 0

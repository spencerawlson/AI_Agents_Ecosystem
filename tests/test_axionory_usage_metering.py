"""Axionory Phase 4: usage metering — the core.llm hook, the
collecting_usage() contextvar, and persistence keyed to a job id."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

import core.llm as core_llm
import ventoryn.autopilot as autopilot
import ventoryn.saas_routes as saas_routes
import ventoryn.site_jobs as site_jobs
from _signin import operator_store_and_config, sign_in
from core.llm import LLMGateway
from ventoryn.app import create_app
from ventoryn.usage_metering import (
    collecting_usage,
    persist_usage_events,
    record_llm_usage,
)

from test_llm import _clear_keys, install_fake  # noqa: F401
from test_saas_shell import _account, store  # noqa: F401


# -- the collector itself --------------------------------------------------

def test_record_llm_usage_is_a_noop_outside_any_collector():
    record_llm_usage({"model": "x", "input_tokens": 1, "output_tokens": 1,
                      "cost_usd": 0.01})  # must not raise


def test_collecting_usage_collects_only_inside_the_block():
    with collecting_usage() as events:
        record_llm_usage({"model": "a", "input_tokens": 1,
                          "output_tokens": 1, "cost_usd": 0.01})
    record_llm_usage({"model": "b", "input_tokens": 1,
                      "output_tokens": 1, "cost_usd": 0.01})  # after exit
    assert len(events) == 1 and events[0]["model"] == "a"


def test_nested_jobs_do_not_leak_into_each_others_collector():
    with collecting_usage() as outer:
        record_llm_usage({"model": "outer-call"})
        with collecting_usage() as inner:
            record_llm_usage({"model": "inner-call"})
        record_llm_usage({"model": "outer-call-2"})
    assert [e["model"] for e in inner] == ["inner-call"]
    assert [e["model"] for e in outer] == ["outer-call", "outer-call-2"]


# -- the real LLMGateway hook, end to end -----------------------------------

def test_real_gateway_call_is_recorded_through_the_hook(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    install_fake(monkeypatch, {"ok": True}, cost=0.004)
    core_llm.set_usage_hook(record_llm_usage)
    try:
        with collecting_usage() as events:
            LLMGateway().complete("hi", tier="cheap")
        assert len(events) == 1
        assert events[0]["model"] == "gpt-6-luna"
        assert events[0]["input_tokens"] == 120
        assert events[0]["output_tokens"] == 60
        assert events[0]["cost_usd"] == pytest.approx(0.004)
    finally:
        core_llm.set_usage_hook(None)


def test_hook_failure_never_breaks_the_actual_llm_call(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    install_fake(monkeypatch, {"ok": True})
    core_llm.set_usage_hook(lambda info: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        out = LLMGateway().complete("hi")  # must not raise
        assert out["json"] == {"ok": True}
    finally:
        core_llm.set_usage_hook(None)


# -- persistence -------------------------------------------------------------

def test_persist_records_cost_in_cents_and_parses_provider(store):
    account = _account(store)
    events = [{"model": "openai/gpt-x", "input_tokens": 10,
              "output_tokens": 20, "cost_usd": 0.0523}]
    n = persist_usage_events(store, events, account_id=account["id"],
                             job_id="job_1", site_id="site_1")
    assert n == 1
    rows = store.usage_events_for_job("job_1")
    assert len(rows) == 1
    assert rows[0]["provider"] == "openai"
    assert rows[0]["model"] == "openai/gpt-x"
    assert rows[0]["provider_cost_cents"] == 5  # round(5.23) cents
    assert rows[0]["customer_charge_cents"] is None  # Phase 5 prices this


def test_persist_is_idempotent_per_job(store):
    account = _account(store)
    events = [{"model": "m", "input_tokens": 1, "output_tokens": 1,
              "cost_usd": 0.01}]
    persist_usage_events(store, events, account_id=account["id"], job_id="job_2")
    n = persist_usage_events(store, events, account_id=account["id"], job_id="job_2")
    assert n == 0
    assert len(store.usage_events_for_job("job_2")) == 1


def test_persist_with_no_events_or_no_job_id_is_a_noop(store):
    account = _account(store)
    assert persist_usage_events(store, [], account_id=account["id"], job_id="job_3") == 0
    assert persist_usage_events(
        store, [{"model": "m"}], account_id=account["id"], job_id="") == 0


# -- wiring: autopilot.execute() ---------------------------------------------

def test_autopilot_execute_persists_usage_against_the_job(store, monkeypatch):
    from ventoryn.saas import SaasConfig

    config = SaasConfig(stripe_price_id="p", monthly_credits=100)
    account = _account(store)
    store.grant_credits(account["id"], 100, "test")
    site = store.create_site(account["id"], "https://shop.example.com")

    def fake_run(job_type, site, inputs, *, orchestrator, reports_dir, store=None):
        record_llm_usage({"model": "gpt-x", "input_tokens": 5,
                          "output_tokens": 5, "cost_usd": 0.02})
        return "done", "report.md"

    monkeypatch.setattr(autopilot.site_jobs, "run", fake_run)
    job = autopilot.execute(store, config, account, site, "analyze",
                            orchestrator=None, reports_dir=Path("."))
    rows = store.usage_events_for_job(job["id"])
    assert len(rows) == 1 and rows[0]["model"] == "gpt-x"


def test_autopilot_execute_persists_usage_even_when_the_job_fails(store, monkeypatch):
    from ventoryn.saas import SaasConfig

    config = SaasConfig(stripe_price_id="p", monthly_credits=100)
    account = _account(store)
    store.grant_credits(account["id"], 100, "test")
    site = store.create_site(account["id"], "https://shop.example.com")

    def fake_run(job_type, site, inputs, *, orchestrator, reports_dir, store=None):
        record_llm_usage({"model": "gpt-x", "input_tokens": 5,
                          "output_tokens": 5, "cost_usd": 0.02})
        raise RuntimeError("provider call succeeded but the job failed after")

    monkeypatch.setattr(autopilot.site_jobs, "run", fake_run)
    job = autopilot.execute(store, config, account, site, "analyze",
                            orchestrator=None, reports_dir=Path("."))
    assert job["status"] == "failed"
    rows = store.usage_events_for_job(job["id"])
    assert len(rows) == 1  # the real provider cost is not lost


# -- wiring: the manual job route --------------------------------------------

def test_manual_job_route_persists_usage_against_the_job(tmp_path, monkeypatch):
    store, config = operator_store_and_config()
    app = create_app(saas_store=store, saas_config=config)
    client = TestClient(app)
    account = sign_in(client, store, email="ada@example.com", sub="sub-ada",
                      name="Ada")
    store.grant_credits(account["id"], 100, "test grant")
    site = store.create_site(account["id"], "https://example.com")
    monkeypatch.setattr(saas_routes, "REPORTS_DIR", tmp_path)

    def fake_run(job_type, site, inputs, *, orchestrator, reports_dir, store=None):
        record_llm_usage({"model": "gpt-x", "input_tokens": 1,
                          "output_tokens": 1, "cost_usd": 0.01})
        return "done", "report.md"

    monkeypatch.setattr(site_jobs, "run", fake_run)
    r = client.post(f"/websites/sites/{site['id']}/jobs",
                    data={"job_type": "analyze"})
    assert r.status_code == 200
    job = store.list_jobs(account["id"])[0]
    rows = store.usage_events_for_job(job["id"])
    assert len(rows) == 1 and rows[0]["model"] == "gpt-x"

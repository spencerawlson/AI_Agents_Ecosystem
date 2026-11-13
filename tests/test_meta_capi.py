"""Meta Conversions API: Axionory's own sign-ups and payments go to Meta,
hashed, server-side; never for the operator, never after an opt-out."""

import hashlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from test_saas_shell import _account, _fake_google_urlopen, _login, client, config, store  # noqa: F401
from ventoryn import meta_capi
from ventoryn.saas import handle_platform_webhook


def _sha(v):
    return hashlib.sha256(v.encode()).hexdigest()


@pytest.fixture()
def sent(monkeypatch):
    monkeypatch.setenv("META_CAPI_DATASET_ID", "123456")
    monkeypatch.setenv("META_CAPI_ACCESS_TOKEN", "capi-token")
    out = []
    monkeypatch.setattr(meta_capi, "_dispatch", out.append)
    return out


def test_event_is_hashed_and_carries_no_raw_email():
    ev = meta_capi.build_event("Subscribe", event_id="evt_1",
                               account={"id": "acc1", "email": " Ada@Example.com "},
                               action_source="system_generated", value_cents=2000)
    assert ev["user_data"]["em"] == [_sha("ada@example.com")]
    assert ev["user_data"]["external_id"] == [_sha("acc1")]
    assert ev["custom_data"] == {"value": 20.0, "currency": "USD"}
    assert "ada@example.com" not in json.dumps(ev).lower()


def test_send_posts_to_the_dataset_with_the_test_code():
    seen = []

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=20):
        seen.append(req)
        return R(b'{"events_received": 1}')
    env = {"META_CAPI_DATASET_ID": "999", "META_CAPI_ACCESS_TOKEN": "t",
           "META_CAPI_TEST_CODE": "TEST123"}
    assert meta_capi.send([{"event_name": "Purchase"}], env=env, urlopen=urlopen) == {"events_received": 1}
    body = json.loads(seen[0].data)
    assert seen[0].full_url == "https://graph.facebook.com/v24.0/999/events"
    assert body["test_event_code"] == "TEST123" and body["data"][0]["event_name"] == "Purchase"


def test_new_sign_up_sends_complete_registration(client, store, sent, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_google_urlopen)
    from urllib.parse import parse_qs, urlparse

    r = client.get("/account/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.get("/account/auth/google/callback", params={"code": "c", "state": state},
               headers={"user-agent": "UA/1", "cf-connecting-ip": "203.0.113.9"},
               follow_redirects=False)
    assert [e["event_name"] for e in sent] == ["CompleteRegistration"]
    ev = sent[0]
    assert ev["action_source"] == "website"
    assert ev["user_data"]["client_user_agent"] == "UA/1"
    assert ev["user_data"]["client_ip_address"] == "203.0.113.9"
    assert ev["user_data"]["em"] == [_sha("ada@example.com")]


def test_subscription_renewal_and_topup_are_sent(store, config, sent):
    acct = _account(store)
    handle_platform_webhook(store, config, {"id": "evt_sub", "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": acct["id"], "customer": "cus_1",
                            "subscription": "sub_1", "amount_total": 2000, "currency": "usd"}}})
    handle_platform_webhook(store, config, {"id": "evt_ren", "type": "invoice.payment_succeeded",
        "data": {"object": {"subscription": "sub_1", "customer": "cus_1", "amount_paid": 2000,
                            "currency": "usd", "billing_reason": "subscription_cycle"}}})
    handle_platform_webhook(store, config, {"id": "evt_top", "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": acct["id"], "amount_total": 5000,
                            "currency": "usd", "payment_intent": "pi_1",
                            "metadata": {"kind": "wallet_topup"}}}})
    assert [(e["event_name"], e["event_id"], e["custom_data"]["value"]) for e in sent] == [
        ("Subscribe", "evt_sub", 20.0), ("Purchase", "evt_ren", 20.0), ("Purchase", "evt_top", 50.0)]
    # a redelivered webhook is not sent twice
    handle_platform_webhook(store, config, {"id": "evt_ren", "type": "invoice.payment_succeeded",
        "data": {"object": {"subscription": "sub_1", "amount_paid": 2000,
                            "billing_reason": "subscription_cycle"}}})
    assert len(sent) == 3


def test_operator_opt_out_and_unconfigured_send_nothing(store, config, sent, monkeypatch):
    config.operator_email = "owner@example.com"
    assert config.is_operator("owner@example.com")
    op = store.create_account("sub-op", "owner@example.com", "Op")
    assert not meta_capi.track(store, config, op, "Purchase", event_id="e1", value_cents=100)
    acct = _account(store)
    store.set_ad_measurement_opt_out(acct["id"], True)
    assert not meta_capi.track(store, config, acct, "Purchase", event_id="e2", value_cents=100)
    store.set_ad_measurement_opt_out(acct["id"], False)
    monkeypatch.delenv("META_CAPI_ACCESS_TOKEN")
    assert not meta_capi.track(store, config, acct, "Purchase", event_id="e3", value_cents=100)
    assert sent == []


def test_security_page_opt_out_switch_and_privacy_policy(client, store, sent):
    acct = _login(client, store)
    page = client.get("/account/security").text
    assert "Ad measurement" in page and "Opt out" in page
    client.post("/account/security/ad-measurement", data={"opt_out": "1"})
    assert store.get_account(acct["id"])["ad_measurement_opt_out"] == 1
    assert "Turn ad measurement on" in client.get("/account/security").text
    privacy = client.get("/privacy").text
    assert "Meta (ad measurement)" in privacy and "Ad measurement" in privacy

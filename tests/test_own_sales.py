"""ventoryn.app counts its own subscribers as paying customers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_saas_shell import _account, _signed, client, config, store  # noqa: F401
from ventoryn import saas


def _checkout_event(account, site_id, event_id="evt_own_1"):
    return {"id": event_id, "type": "checkout.session.completed",
            "data": {"object": {"client_reference_id": account["id"],
                                "customer": "cus_1", "subscription": "sub_1",
                                "amount_total": 2000, "currency": "usd",
                                "metadata": {"ventoryn_site_id": site_id}}}}


def test_subscription_counts_once_for_the_own_site(client, store, monkeypatch):
    owner = _account(store, sub="sub-owner", email="owner@example.com")
    own = store.create_site(owner["id"], "https://ventoryn.app")
    monkeypatch.setenv("VENTORYN_OWN_SITE_ID", own["id"])
    buyer = _account(store)
    payload, headers = _signed("whsec_platform", _checkout_event(buyer, own["id"]))
    r = client.post("/account/billing/webhook", content=payload, headers=headers)
    assert r.json()["result"] == "ok"
    assert store.get_account(buyer["id"])["subscription_status"] == "active"
    assert store.paying_customer_count(own["id"]) == 1
    client.post("/account/billing/webhook", content=payload, headers=headers)
    assert store.paying_customer_count(own["id"]) == 1  # redelivery


def test_other_site_ids_in_platform_events_are_not_counted(client, store, monkeypatch):
    owner = _account(store, sub="sub-owner", email="owner@example.com")
    own = store.create_site(owner["id"], "https://ventoryn.app")
    other = store.create_site(owner["id"], "https://shop.example.com")
    monkeypatch.setenv("VENTORYN_OWN_SITE_ID", own["id"])
    buyer = _account(store)
    payload, headers = _signed("whsec_platform", _checkout_event(buyer, other["id"]))
    client.post("/account/billing/webhook", content=payload, headers=headers)
    assert store.paying_customer_count(other["id"]) == 0
    assert store.paying_customer_count(own["id"]) == 0


def test_checkout_session_carries_own_site_id(config, monkeypatch):
    monkeypatch.setenv("VENTORYN_OWN_SITE_ID", "aaaaaaaaaaaaaaaaaaaaaaaa")
    sent = {}

    def fake_post(url, fields, headers=None, urlopen=None):
        sent.update(fields)
        return {"id": "cs_1", "url": "https://checkout.stripe.com/x"}

    monkeypatch.setattr(saas, "_http_post_form", fake_post)
    saas.create_checkout_session(config, {"id": "acc", "email": "a@example.com"})
    assert sent["metadata[ventoryn_site_id]"] == "aaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setenv("VENTORYN_OWN_SITE_ID", "off")
    sent.clear()
    saas.create_checkout_session(config, {"id": "acc", "email": "a@example.com"})
    assert "metadata[ventoryn_site_id]" not in sent

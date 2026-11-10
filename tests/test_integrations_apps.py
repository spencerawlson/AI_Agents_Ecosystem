"""Integrations: apps grid -> an app's websites -> a window per website."""

import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _signin import operator_store_and_config, sign_in
from ventoryn.app import create_app
from ventoryn.saas_routes import X_RETURN_COOKIE, _integrations_return


def _client(**config_overrides):
    store, config = operator_store_and_config()
    config = replace(config, **config_overrides)
    client = TestClient(create_app(saas_store=store, saas_config=config))
    account = sign_in(client, store, email="ada@example.com", sub="sub-ada", name="Ada")
    return client, store, account


def test_grid_lists_apps_with_counts():
    client, store, account = _client()
    a = store.create_site(account["id"], "https://shop.example.com")
    store.create_site(account["id"], "https://blog.example.org")
    store.update_connection(a["id"], "stripe_sales", "connected", "Connected.",
                            {"webhook_secret": "whsec_test"})
    html = client.get("/integrations").text
    for name in ("X", "Stripe sales", "Plausible", "Reddit"):
        assert f"<span class='v-app-name'>{name}</span>" in html
    assert "href='/integrations/stripe_sales'" in html
    assert "Connected on 1 of 2 websites" in html
    assert "href='/integrations/reddit'" not in html  # not available


def test_app_page_lists_websites_each_with_a_window():
    client, store, account = _client()
    a = store.create_site(account["id"], "https://shop.example.com")
    b = store.create_site(account["id"], "https://blog.example.org")
    html = client.get("/integrations/plausible").text
    for s in (a, b):
        assert f"data-open='dlg-{s['id']}'" in html
        assert f"<dialog class='v-dialog' id='dlg-{s['id']}'" in html
        assert f"action='/account/sites/{s['id']}/connections/plausible'" in html
    assert "name='return_to' value='/integrations/plausible?site=" in html
    assert "type='password'" in html  # keys are never echoed while typing


def test_site_param_reopens_that_window():
    client, store, account = _client()
    a = store.create_site(account["id"], "https://shop.example.com")
    html = client.get(f"/integrations/stripe_sales?site={a['id']}").text
    assert f"getElementById('dlg-{a['id']}')" in html
    other = client.get("/integrations/stripe_sales?site=not-mine").text
    assert "getElementById('dlg-not-mine')" not in other


def test_unknown_or_unavailable_app_is_404():
    client, _, _ = _client()
    assert client.get("/integrations/nope").status_code == 404
    assert client.get("/integrations/reddit").status_code == 404


def test_saving_in_a_window_returns_to_the_app():
    client, store, account = _client()
    a = store.create_site(account["id"], "https://shop.example.com")
    back = f"/integrations/stripe_sales?site={a['id']}"
    r = client.post(f"/account/sites/{a['id']}/connections/stripe_sales",
                    data={"webhook_secret": "whsec_abc", "return_to": back},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == back
    assert store.get_connection(a["id"], "stripe_sales")["status"] == "connected"


@pytest.mark.parametrize("evil", ["https://evil.example/x", "//evil.example/x",
                                  "/account/logout", "/integrations\\..\\x",
                                  "/integrations/x\r\nSet-Cookie: a=b"])
def test_return_to_cannot_redirect_elsewhere(evil):
    client, store, account = _client()
    a = store.create_site(account["id"], "https://shop.example.com")
    r = client.post(f"/account/sites/{a['id']}/connections/stripe_sales",
                    data={"webhook_secret": "whsec_abc", "return_to": evil},
                    follow_redirects=False)
    assert r.headers["location"] == f"/account/sites/{a['id']}"
    assert _integrations_return(evil) is None


def test_disconnect_in_a_window_returns_to_the_app():
    client, store, account = _client()
    a = store.create_site(account["id"], "https://shop.example.com")
    store.update_connection(a["id"], "plausible", "connected", "Connected.",
                            {"api_key": "k", "site_domain": "shop.example.com"})
    back = f"/integrations/plausible?site={a['id']}"
    from _signin import fresh_totp_code
    r = client.post(f"/account/sites/{a['id']}/connections/plausible/disconnect",
                    data={"return_to": back,
                          "totp_code": fresh_totp_code(store, account["id"])},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == back
    assert store.get_connection(a["id"], "plausible")["status"] != "connected"


def test_x_connect_remembers_the_window_across_the_round_trip():
    client, store, account = _client(x_client_id="cid", x_client_secret="sec",
                                     x_app_registered=True)
    a = store.create_site(account["id"], "https://shop.example.com")
    back = f"/integrations/x?site={a['id']}"
    r = client.get(f"/account/sites/{a['id']}/connections/x/start",
                   params={"return_to": back}, follow_redirects=False)
    assert r.status_code == 303 and "twitter.com" in r.headers["location"]
    from urllib.parse import unquote
    assert unquote(r.cookies.get(X_RETURN_COOKIE).strip('"')) == back
    evil = client.get(f"/account/sites/{a['id']}/connections/x/start",
                      params={"return_to": "https://evil.example"},
                      follow_redirects=False)
    assert X_RETURN_COOKIE not in evil.cookies

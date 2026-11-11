"""Websites: hidden revenue, revealed on demand by the eye button."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from _signin import operator_store_and_config, sign_in
from ventoryn.app import create_app


def _setup():
    store, config = operator_store_and_config()
    client = TestClient(create_app(saas_store=store, saas_config=config))
    account = sign_in(client, store, email="ada@example.com", sub="sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://shop.example.com")
    store.update_connection(site["id"], "stripe_sales", "connected", "Connected.",
                            {"webhook_secret": "whsec_x"})
    store.record_conversion(site["id"], 4900, "usd", "manual")
    return client, store, account, site


def test_page_keeps_revenue_hidden_until_the_eye_is_clicked():
    client, _, _, site = _setup()
    html = client.get("/portfolio").text
    assert "data-rev-eye" in html and "aria-label='Show revenue'" in html
    assert f"<span data-rev-site='{site['id']}'>hidden</span>" in html
    assert "$49.00" not in html  # the figure is never in the page itself


def test_eye_endpoint_returns_the_owners_revenue_uncached():
    client, _, _, site = _setup()
    r = client.get("/api/websites/revenue")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["sites"] == {site["id"]: "$49.00"}
    assert "49" in data["total"] and "49" in data["net_profit"]


def test_eye_endpoint_needs_a_session_and_only_shows_own_sites():
    client, store, _, site = _setup()
    anon = TestClient(client.app)
    assert anon.get("/api/websites/revenue").status_code == 401
    other = TestClient(client.app)
    sign_in(other, store, email="bo@example.com", sub="sub-bo", name="Bo")
    assert site["id"] not in other.get("/api/websites/revenue").json()["sites"]


def test_saved_privacy_setting_is_unchanged_by_reveal():
    client, store, _, site = _setup()
    client.get("/api/websites/revenue")
    assert not store.get_site(site["id"]).get("revenue_show")
    assert store.revenue_for_site(site["id"])["revenue"] is None

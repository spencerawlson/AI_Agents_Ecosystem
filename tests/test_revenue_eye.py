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
    # One toggle, in the header in front of the bell (no per-card eyes).
    assert "id='rev-toggle'" in html and "aria-label='Show revenue'" in html
    assert html.index("id='rev-toggle'") < html.index("aria-label='Approvals'")
    assert "data-rev-eye" not in html and "class='v-eye'" not in html
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


def test_toggle_starts_hidden_and_only_shows_where_revenue_is_hidden():
    client, _, _, _ = _setup()
    html = client.get("/portfolio").text
    assert "<button class='rev-toggle' id='rev-toggle' type='button' role='switch' hidden aria-checked='false'" in html
    assert "<span class='rev-track' aria-hidden='true'><span class='rev-knob'></span></span>" in html
    assert "querySelectorAll('[data-rev-total],[data-rev-profit],[data-rev-site],[data-rev-site-profit]')" in html


def test_overview_cards_can_be_revealed_by_the_switch():
    client, _, _, site = _setup()
    html = client.get("/ops").text
    assert "id='rev-toggle'" in html
    assert f"<span data-rev-site='{site['id']}'>—</span>" in html
    assert f"<span data-rev-site-profit='{site['id']}'>—</span>" in html
    assert "$49.00" not in html


def test_costs_are_credits_used_at_the_plan_rate():
    client, store, account, site = _setup()
    store.grant_credits(account["id"], 100, "test")
    job = store.charge_credits(account["id"], "seo_article", site["id"])  # 10 credits
    store.set_job_result(job["id"], "done", "ok")
    failed = store.charge_credits(account["id"], "analyze", site["id"])
    store.set_job_result(failed["id"], "failed", "refunded")
    assert store.site_credits_used(site["id"]) == 10
    html = client.get("/portfolio").text
    assert "<span class='v-stat-label'>Costs</span><span class='v-stat-value'>$0.20</span>" in html
    data = client.get("/api/websites/revenue").json()
    assert data["site_profit"] == {site["id"]: "$48.80"}
    assert data["net_profit"] == "$48.80"


def test_overview_shows_revenue_costs_and_net_profit():
    client, _, _, _ = _setup()
    html = client.get("/ops").text
    for label in ("Revenue", "Costs", "Net profit"):
        assert f"<span class='v-stat-label'>{label}</span>" in html
    assert "data-rev-total>—</span>" in html and "data-rev-profit>—</span>" in html

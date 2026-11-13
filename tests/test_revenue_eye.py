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
    assert html.index("id='rev-toggle'") < html.index("id='bell-btn'")
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


def test_costs_are_what_the_platform_apis_charged():
    client, store, account, site = _setup()
    store.grant_credits(account["id"], 100, "test")
    job = store.charge_credits(account["id"], "seo_article", site["id"])  # credits, not a cost
    store.set_job_result(job["id"], "done", "ok")
    store.record_x_post_cost(site["id"], "d1", 20)                       # X: $0.20
    store.record_social_spend(account["id"], site["id"], "facebook", "d2", 0.05)
    html = client.get("/portfolio").text
    assert "<span class='v-stat-label'>Costs</span><span class='v-stat-value'>$0.25</span>" in html
    data = client.get("/api/websites/revenue").json()
    assert data["site_profit"] == {site["id"]: "$48.75"}
    assert data["net_profit"] == "$48.75"


def test_owner_credits_go_down_as_jobs_run():
    import re
    from _signin import OPERATOR_EMAIL

    store, config = operator_store_and_config()
    client = TestClient(create_app(saas_store=store, saas_config=config))
    account = sign_in(client, store, email=OPERATOR_EMAIL, sub="sub-op", name="Op")
    site = store.create_site(account["id"], "https://shop.example.com")
    def credits():
        html = client.get("/ops").text
        return int(re.search(r"Credits</span><span class='v-stat-value'>([\d,]+)<", html)
                   .group(1).replace(",", ""))
    before = credits()
    job = store.charge_credits(account["id"], "seo_article", site["id"], is_operator=True)
    store.set_job_result(job["id"], "done", "ok")
    assert credits() == before - 10


def test_overview_shows_revenue_costs_and_net_profit():
    client, _, _, _ = _setup()
    html = client.get("/ops").text
    for label in ("Revenue", "Costs", "Net profit"):
        assert f"<span class='v-stat-label'>{label}</span>" in html
    assert "data-rev-total>—</span>" in html and "data-rev-profit>—</span>" in html


def test_toggle_still_renders_when_revenue_is_fully_visible():
    """The toggle is not only for hidden revenue: on a page whose
    figures are all visible it still renders, and its script masks
    them on load (remembering the real figures in data-real) so OFF
    really hides them."""
    client, store, _, site = _setup()
    store.set_revenue_show(site["id"], True)
    html = client.get("/portfolio").text
    assert "id='rev-toggle'" in html
    assert "$49.00" in html  # the server renders the real figure...
    # ...and the toggle script remembers it, then masks every cell.
    assert "data-real" in html
    assert "el.textContent='—'" in html
    # The eye endpoint still returns the same figures on demand.
    assert client.get("/api/websites/revenue").json()["sites"] == {
        site["id"]: "$49.00"}


def test_toggle_masks_visible_revenue_on_overview():
    """Overview with a visible site: the stats card shows the real
    total in the HTML (the server chose to show it) and the toggle
    script masks it on load; hide the site again and the figure must
    vanish from the page entirely."""
    client, store, _, site = _setup()
    store.set_revenue_show(site["id"], True)
    html = client.get("/ops").text
    assert "id='rev-toggle'" in html
    assert "$49.00" in html
    assert "data-rev-total" in html
    store.set_revenue_show(site["id"], False)
    hidden = client.get("/ops").text
    assert "$49.00" not in hidden
    assert "data-rev-total" in hidden


def test_reveal_choice_persists_across_pages():
    """The toggle remembers the choice in sessionStorage so a reveal
    survives navigation within the session."""
    client, _, _, _ = _setup()
    html = client.get("/portfolio").text
    assert "ventoryn.revealRevenue" in html
    assert "sessionStorage.getItem" in html
    assert "if(keep)show();" in html

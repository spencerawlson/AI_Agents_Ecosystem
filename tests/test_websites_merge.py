"""Websites page absorbs My account's "Your websites" and "Recent jobs"."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _signin import operator_store_and_config, sign_in
from ventoryn.app import create_app


def _client():
    store, config = operator_store_and_config()
    app = create_app(saas_store=store, saas_config=config)
    client = TestClient(app)
    account = sign_in(client, store, email="ada@example.com", sub="sub-ada", name="Ada")
    return client, store, account


def test_site_card_carries_the_funnel_and_link():
    client, store, account = _client()
    site = store.create_site(account["id"], "https://shop.example.com")
    html = client.get("/portfolio").text
    # One row per website: key numbers here, full funnel on its page.
    assert "Visitors (30 days)" in html and "Paying customers" in html
    # Open goes to the website's page in Ventoryn, never off-site.
    assert f"<td><a href='/websites/sites/{site['id']}'>Open</a></td>" in html
    assert "href='https://shop.example.com' target='_blank'" not in html
    assert (f"<a class='v-site-name' href='/websites/sites/{site['id']}'>"
            f"shop.example.com</a>" in html)


def test_recent_jobs_on_websites_with_site_column():
    client, store, account = _client()
    site = store.create_site(account["id"], "https://shop.example.com")
    store.grant_credits(account["id"], 50, "test")
    job = store.charge_credits(account["id"], "geo_audit", site["id"])
    store.set_job_result(job["id"], "done", "GEO 70/100")
    html = client.get("/portfolio").text
    assert "<h2>Recent jobs</h2>" in html
    assert "<th>Website</th>" in html
    assert "shop.example.com</td><td>geo audit</td><td>done</td>" in html


def test_my_account_no_longer_lists_websites_or_jobs():
    client, store, account = _client()
    store.create_site(account["id"], "https://shop.example.com")
    html = client.get("/account/dashboard").text
    # The page body only — the header's alerts panel may name the site.
    main = html.split("<main", 1)[-1]
    assert "Your websites" not in main and "Recent jobs" not in main
    assert "shop.example.com" not in main
    assert "Add a website" in html  # the plan card keeps the way in


def test_website_page_highlights_websites_not_my_account():
    client, store, account = _client()
    site = store.create_site(account["id"], "https://shop.example.com")
    nav = client.get(f"/websites/sites/{site['id']}").text.split("<main", 1)[0]
    # Websites is highlighted and still links back to the list.
    assert "<a class='nav-item active' aria-current='location' href='/portfolio'>" in nav
    assert "aria-current='page'" not in nav  # My account is not the current page

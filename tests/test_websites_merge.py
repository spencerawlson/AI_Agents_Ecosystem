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
    assert (f"href='https://shop.example.com' target='_blank' "
            f"rel='noopener noreferrer'>Open</a>" in html)
    assert (f"<a class='v-site-name' href='/account/sites/{site['id']}'>"
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
    assert "Your websites" not in html and "Recent jobs" not in html
    assert "shop.example.com" not in html
    assert "Add a website" in html  # the plan card keeps the way in

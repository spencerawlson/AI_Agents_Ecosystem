"""Websites: the list scrolls, and gets a filter once it is long."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from _signin import operator_store_and_config, sign_in
from ventoryn.app import create_app


def _page(n):
    store, config = operator_store_and_config()
    client = TestClient(create_app(saas_store=store, saas_config=config))
    account = sign_in(client, store, email="ada@example.com", sub="sub-ada", name="Ada")
    for i in range(n):
        store.create_site(account["id"], f"https://site{i}.example.com")
    return client.get("/portfolio").text


def test_websites_sit_in_a_scrollable_region():
    html = _page(3)
    assert "<div class='v-site-list' id='site-list' tabindex='0' role='region'" in html
    assert ".v-site-list { max-height:" in html and "overflow-y: auto" in html
    assert "id='site-filter'" not in html  # short list: no filter needed


def test_long_list_gets_a_filter():
    html = _page(20)
    assert "id='site-filter'" in html and "Filter 20 websites" in html
    assert html.count("<article class='v-biz") >= 20


def test_no_websites_still_shows_the_empty_state():
    assert "Add your first website" in _page(0)

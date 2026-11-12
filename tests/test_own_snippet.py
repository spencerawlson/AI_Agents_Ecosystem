"""Axionory tracks itself with Axionory Analytics (site 8e7c68c8885a1ef3e072a01c)."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _signin import operator_store_and_config, sign_in
from ventoryn.app import create_app

TAG = '<script defer data-site="8e7c68c8885a1ef3e072a01c" src="https://ventoryn.app/v.js"></script>'


def _client():
    store, config = operator_store_and_config()
    client = TestClient(create_app(saas_store=store, saas_config=config))
    return client, store


def test_landing_and_app_pages_carry_the_tag():
    client, store = _client()
    landing = client.get("/").text
    assert TAG in landing and landing.index(TAG) < landing.index("</head>")
    sign_in(client, store)
    for path in ("/ops", "/portfolio", "/account/dashboard"):
        html = client.get(path).text
        assert html.count(TAG) == 1, path
        assert html.index(TAG) < html.index("</head>"), path


def test_tag_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("VENTORYN_OWN_SITE_ID", "off")
    client, _ = _client()
    assert "ventoryn.app/v.js" not in client.get("/").text.split("</head>")[0]

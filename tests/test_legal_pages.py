"""Public Privacy Policy and Terms of Service."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from _signin import operator_store_and_config
from ventoryn.app import create_app


def _client():
    store, config = operator_store_and_config()
    return TestClient(create_app(saas_store=store, saas_config=config))


def test_pages_are_public_and_complete():
    c = _client()
    p = c.get("/privacy")
    assert p.status_code == 200
    for must in ("Privacy Policy", "privacy@ventoryn.app", "Spencer Rawlson",
                 "Person in charge of personal information", "Law 25",
                 "outside Quebec", "TikTok", "Commission d"):
        assert must in p.text, must
    t = c.get("/terms")
    assert t.status_code == 200
    for must in ("Terms of Service", "$20 per month", "explicit approval", "laws of Quebec"):
        assert must in t.text, must


def test_linked_from_landing_and_sitemap():
    c = _client()
    home = c.get("/").text
    assert "href=\"/privacy\">Privacy Policy</a>" in home and "href=\"/terms\">Terms of Service</a>" in home
    assert "Privacy Policy — coming soon" not in home
    sm = c.get("/sitemap.xml").text
    assert "/privacy</loc>" in sm and "/terms</loc>" in sm

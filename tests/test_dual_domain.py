"""Sign-in returns to the domain the visitor started on (axionory.com or ventoryn.app)."""

import sys
import urllib.parse
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from ventoryn.app import create_app
from ventoryn.saas import SaasConfig, SaasStore


def _config(**kw):
    return SaasConfig(google_client_id="gid", google_client_secret="gs",
                      base_url="https://www.axionory.com",
                      extra_base_urls=("https://ventoryn.app",), **kw)


def _redirect_uri(client, host):
    r = client.get("/account/auth/google/start", headers={"host": host}, follow_redirects=False)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    return q["redirect_uri"][0]


def test_each_domain_gets_its_own_callback():
    client = TestClient(create_app(saas_store=SaasStore(":memory:"), saas_config=_config()))
    assert _redirect_uri(client, "ventoryn.app") == "https://ventoryn.app/account/auth/google/callback"
    assert _redirect_uri(client, "www.axionory.com") == "https://www.axionory.com/account/auth/google/callback"


def test_unknown_host_never_becomes_a_callback():
    client = TestClient(create_app(saas_store=SaasStore(":memory:"), saas_config=_config()))
    assert _redirect_uri(client, "evil.example") == "https://www.axionory.com/account/auth/google/callback"


def test_env_parsing_keeps_only_https_urls():
    cfg = SaasConfig.from_env({"SAAS_BASE_URL": "https://www.axionory.com",
                               "SAAS_EXTRA_BASE_URLS": "https://ventoryn.app/, http://bad.example, "})
    assert cfg.extra_base_urls == ("https://ventoryn.app",)
    assert cfg.base_url_for_host("VENTORYN.APP") == "https://ventoryn.app"

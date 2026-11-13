"""Meta's data deletion + deauthorize callbacks: a signed request from
Meta removes that person's Facebook/Instagram connections."""

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from test_social_wiring import _app_with, _customer_client, _seed_site
from ventoryn.meta_deletion import parse_signed_request

SECRET = "test-fb-secret"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _signed(payload: dict, secret: str = SECRET) -> str:
    body = _b64(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{_b64(sig)}.{body}"


def _connected(store, site, kind, fb_user_id):
    store.update_connection(site["id"], kind, "connected", "ok",
                            {"access_token": f"{kind}-tok", "fb_user_id": fb_user_id})


def test_parse_signed_request_verifies_the_signature():
    good = _signed({"algorithm": "HMAC-SHA256", "user_id": "u1"})
    assert parse_signed_request(good, SECRET)["user_id"] == "u1"
    assert parse_signed_request(good, "other-secret") is None
    assert parse_signed_request(_signed({"algorithm": "none", "user_id": "u1"}), SECRET) is None
    assert parse_signed_request("garbage", SECRET) is None
    assert parse_signed_request(good, "") is None


def test_data_deletion_removes_that_persons_connections_only():
    app, store, config = _app_with(facebook=True)
    client, account = _customer_client(app, store)
    mine = _seed_site(store, account)
    other = _seed_site(store, account, "https://ben.example.com")
    _connected(store, mine, "facebook", "fb-u1")
    _connected(store, mine, "instagram", "fb-u1")
    _connected(store, other, "facebook", "fb-u2")
    meta = TestClient(app)  # Meta's servers: no session, no Origin
    r = meta.post("/meta/data-deletion",
                  data={"signed_request": _signed({"algorithm": "HMAC-SHA256",
                                                   "user_id": "fb-u1"})})
    assert r.status_code == 200
    out = r.json()
    assert out["url"] == f"{config.base_url.rstrip('/')}/meta/data-deletion/status?code={out['confirmation_code']}"
    for kind in ("facebook", "instagram"):
        conn = store.get_connection(mine["id"], kind)
        assert conn["status"] == "not_connected" and not conn["_detail_raw"].get("access_token")
    assert store.get_connection(other["id"], "facebook")["status"] == "connected"
    page = meta.get(f"/meta/data-deletion/status?code={out['confirmation_code']}")
    assert page.status_code == 200 and "Completed." in page.text and "2 connections" in page.text
    assert meta.get("/meta/data-deletion/status?code=nope").status_code == 404


def test_forged_request_changes_nothing():
    app, store, _config = _app_with(facebook=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    _connected(store, site, "facebook", "fb-u1")
    meta = TestClient(app)
    forged = _signed({"algorithm": "HMAC-SHA256", "user_id": "fb-u1"}, "attacker")
    for path in ("/meta/data-deletion", "/meta/deauthorize"):
        assert meta.post(path, data={"signed_request": forged}).status_code == 400
    assert store.get_connection(site["id"], "facebook")["status"] == "connected"


def test_deauthorize_removes_the_tokens():
    app, store, _config = _app_with(facebook=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    _connected(store, site, "instagram", "fb-u9")
    r = TestClient(app).post("/meta/deauthorize", data={
        "signed_request": _signed({"algorithm": "HMAC-SHA256", "user_id": "fb-u9"})})
    assert r.status_code == 200
    assert store.get_connection(site["id"], "instagram")["status"] == "not_connected"

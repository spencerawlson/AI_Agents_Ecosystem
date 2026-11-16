"""Founding Customer waitlist: GET /founding-customer-waitlist (form
page, email only) + POST (validate, reject duplicates, record).

The landing page's "Join the Founding Customer Waitlist" CTAs point
at this route. Joining is free registration, separate from the
$20/month subscription — nothing is charged and no email is sent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from test_landing import _app_with

URL = "/founding-customer-waitlist"


def _client():
    app, store = _app_with()
    return TestClient(app), store


def test_waitlist_page_renders():
    client, _store = _client()
    r = client.get(URL)
    assert r.status_code == 200
    assert "Join the Founding Customer Waitlist" in r.text
    assert "Free registration, separate from the $20/month subscription" in r.text
    assert "Nothing is charged" in r.text
    assert f"action='{URL}'" in r.text or f'action="{URL}"' in r.text
    assert "type='email'" in r.text or 'type="email"' in r.text
    assert "name='email'" in r.text or 'name="email"' in r.text


def test_waitlist_signup_records_email():
    client, store = _client()
    r = client.post(URL, data={"email": "founder@example.com"})
    assert r.status_code == 200
    assert "You're on the list" in r.text
    assert "founder@example.com" in r.text
    assert "nothing was charged" in r.text
    entry = store.waitlist_entry("founder@example.com")
    assert entry is not None
    assert entry["email"] == "founder@example.com"
    assert entry["source"] == "landing"
    assert entry["created_at"]


def test_waitlist_duplicate_is_idempotent():
    client, store = _client()
    first = client.post(URL, data={"email": "founder@example.com"})
    assert first.status_code == 200
    second = client.post(URL, data={"email": "founder@example.com"})
    assert second.status_code == 200
    assert "already registered" in second.text
    assert "nothing was charged" in second.text
    assert len(store.list_waitlist_entries()) == 1


def test_waitlist_rejects_bad_email():
    client, store = _client()
    for bad in ("", "not-an-email", "a@b", "a b@c.com", "a@b@c.com"):
        r = client.post(URL, data={"email": bad})
        assert r.status_code == 400, bad
        assert "valid email address" in r.text
    assert store.list_waitlist_entries() == []


def test_waitlist_email_is_lowercased():
    client, store = _client()
    r = client.post(URL, data={"email": "  Founder@Example.COM  "})
    assert r.status_code == 200
    assert store.waitlist_entry("founder@example.com") is not None
    assert store.waitlist_entry("Founder@Example.COM") is not None
    assert len(store.list_waitlist_entries()) == 1


def test_waitlist_landing_cta_target_is_live():
    client, _store = _client()
    landing = client.get("/")
    assert landing.status_code == 200
    assert 'href="/founding-customer-waitlist"' in landing.text
    assert client.get("/founding-customer-waitlist").status_code == 200


def test_waitlist_store_methods():
    _client_app, store = _client()
    assert store.waitlist_entry("nobody@example.com") is None
    assert store.list_waitlist_entries() == []
    entry = store.add_waitlist_entry("a@example.com")
    assert entry["email"] == "a@example.com"
    assert store.add_waitlist_entry("a@example.com") == entry
    store.add_waitlist_entry("b@example.com", source="test")
    entries = store.list_waitlist_entries()
    assert [e["email"] for e in entries] == ["a@example.com", "b@example.com"]
    assert entries[1]["source"] == "test"


def test_waitlist_rate_limit():
    client, store = _client()
    for i in range(10):
        r = client.post(URL, data={"email": f"user{i}@example.com"})
        assert r.status_code == 200, i
    limited = client.post(URL, data={"email": "user10@example.com"})
    assert limited.status_code == 429
    assert "Too many waitlist sign-ups" in limited.text
    assert store.waitlist_entry("user10@example.com") is None

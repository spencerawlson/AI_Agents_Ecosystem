"""IndexNow: the key verification file and the submission call."""

import sys
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _signin import operator_store_and_config
from ventoryn.app import create_app
from ventoryn.indexnow import submit


def _client(**overrides):
    store, config = operator_store_and_config()
    config = replace(config, base_url="https://ventoryn.app", **overrides)
    return TestClient(create_app(saas_store=store, saas_config=config))


def test_no_key_configured_serves_no_verification_file():
    r = _client().get("/some-key.txt")
    assert r.status_code == 404


def test_key_file_served_at_its_own_path():
    r = _client(indexnow_key="abc123").get("/abc123.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text == "abc123"


def test_submit_without_key_fails_honestly():
    _store, config = operator_store_and_config()
    ok, message = submit(config, ["https://ventoryn.app/"])
    assert not ok and "not configured" in message


def test_submit_with_no_urls_fails_honestly():
    _store, config = operator_store_and_config()
    config = replace(config, indexnow_key="abc123")
    ok, message = submit(config, [])
    assert not ok and "No URLs" in message


def test_submit_posts_host_key_and_urls():
    _store, config = operator_store_and_config()
    config = replace(config, base_url="https://ventoryn.app", indexnow_key="abc123")
    seen = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10):
        import json

        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        return FakeResp()

    ok, message = submit(config, ["https://ventoryn.app/"], urlopen=fake_urlopen)
    assert ok and "Submitted 1" in message
    assert seen["body"] == {
        "host": "ventoryn.app", "key": "abc123",
        "keyLocation": "https://ventoryn.app/abc123.txt",
        "urlList": ["https://ventoryn.app/"]}


def test_submit_network_failure_is_reported_not_raised():
    _store, config = operator_store_and_config()
    config = replace(config, indexnow_key="abc123")

    def fake_urlopen(req, timeout=10):
        raise OSError("no route to host")

    ok, message = submit(config, ["https://ventoryn.app/"], urlopen=fake_urlopen)
    assert not ok and "no route to host" in message

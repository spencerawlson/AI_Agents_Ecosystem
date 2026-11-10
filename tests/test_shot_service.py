"""The separate screenshot container: service API and the app's client."""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ventoryn.site_screenshot as shots
from ventoryn import shot_service

TOKEN = "t" * 64
PNG = bytes([0x89]) + b"PNG\r\n\x1a\nfake"


@pytest.fixture()
def service():
    calls = []

    def fake_capture(url, out):
        calls.append(url)
        if "broken" in url:
            return False, "unavailable — the page did not finish loading"
        out.write_bytes(PNG)
        return True, ""

    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 shot_service.make_handler(TOKEN, fake_capture))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", calls
    server.shutdown()


def _post(base, body, token=TOKEN):
    req = urllib.request.Request(base + "/shot", data=json.dumps(body).encode(),
                                 headers={"X-Shot-Token": token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def test_health(service):
    base, _ = service
    with urllib.request.urlopen(base + "/health", timeout=5) as r:
        assert r.status == 200 and r.read() == b"ok"


def test_wrong_token_is_refused(service):
    base, calls = service
    assert _post(base, {"url": "https://example.com"}, token="nope")[0] == 403
    assert calls == []


def test_returns_png(service):
    base, calls = service
    code, ctype, body = _post(base, {"url": "https://example.com/"})
    assert (code, ctype, body) == (200, "image/png", PNG)
    assert calls == ["https://example.com/"]


@pytest.mark.parametrize("body", [{"url": "file:///etc/passwd"}, {"url": ""}, {}])
def test_bad_requests(service, body):
    base, calls = service
    assert _post(base, body)[0] == 400 and calls == []


def test_failure_note_is_passed_back(service):
    base, _ = service
    code, _, body = _post(base, {"url": "https://broken.example.com/"})
    assert code == 422 and "did not finish loading" in json.loads(body)["note"]


def test_service_refuses_to_start_without_token(monkeypatch):
    monkeypatch.delenv("VENTORYN_SHOT_TOKEN", raising=False)
    assert shot_service.main([]) == 2
    monkeypatch.setenv("VENTORYN_SHOT_TOKEN", TOKEN)
    assert shot_service.main(["--host", "0.0.0.0"]) == 2


# -- the app's client ----------------------------------------------------------

def test_client_fetches_from_the_service(service, tmp_path, monkeypatch):
    base, _ = service
    monkeypatch.setattr(shots, "_allowed", lambda url: True)  # no DNS in tests
    out = tmp_path / "s.png"
    assert shots._capture_remote("https://example.com/", out, base, TOKEN) == (True, "")
    assert out.read_bytes() == PNG


def test_client_reports_service_note(service, tmp_path, monkeypatch):
    base, _ = service
    monkeypatch.setattr(shots, "_allowed", lambda url: True)
    ok, note = shots._capture_remote("https://broken.example.com/", tmp_path / "x.png",
                                     base, TOKEN)
    assert not ok and "did not finish loading" in note


def test_client_only_talks_to_a_local_service(tmp_path):
    ok, note = shots._capture_remote("https://example.com/", tmp_path / "x.png",
                                     "http://10.0.0.5:8210", TOKEN)
    assert not ok and "not local" in note


def test_client_when_service_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(shots, "_allowed", lambda url: True)
    ok, note = shots._capture_remote("https://example.com/", tmp_path / "x.png",
                                     "http://127.0.0.1:9", TOKEN, timeout=2)
    assert not ok and "not running" in note

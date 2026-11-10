"""Home-page screenshot for the website snapshot — safely, or not at all.

Opening an untrusted website in a browser on the server is the risky
part of the snapshot, so:

- Chromium runs WITH its sandbox (Playwright disables it by default;
  we turn it back on). If the sandbox cannot start on this host, there
  is no screenshot — never a sandbox-less browser.
- Every request the page makes (documents, images, scripts, XHR/fetch)
  and every WebSocket goes through core.netguard: no loopback, private,
  link-local (cloud metadata) or reserved addresses, http(s)/ws(s) only.
- WebRTC is forced through no local interfaces, service workers and
  downloads are blocked, one capture runs at a time, 20 s limit.

Residual risk, accepted and documented: DNS rebinding between the
guard's lookup and the browser's own lookup. The sandbox is the
second line of defence for that.
"""

from __future__ import annotations

import threading
from pathlib import Path

_LOCK = threading.Lock()
VIEWPORT = {"width": 1280, "height": 800}
_ARGS = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
         "--disable-features=WebRtcHideLocalIpsWithMdns",
         "--disable-dev-shm-usage"]


def _allowed(url: str) -> bool:
    from core.netguard import BlockedAddressError, check_url

    if url.startswith(("data:", "blob:", "about:")):
        return True
    if url.startswith(("ws://", "wss://")):
        url = "http" + url[2:]
    try:
        check_url(url)
        return True
    except BlockedAddressError:
        return False


def capture(url: str, out_path: Path, timeout_ms: int = 20_000) -> tuple[bool, str]:
    """(ok, note). Never raises.

    In production the browser runs in the separate screenshot container
    (ventoryn.shot_service: no data, no secrets, Chrome sandbox on); the
    app calls it when VENTORYN_SHOT_URL is set. Without it (local dev),
    the browser runs in-process — still sandbox-only."""
    import os

    if os.environ.get("VENTORYN_SHOT_URL"):
        return _capture_remote(url, out_path, os.environ["VENTORYN_SHOT_URL"],
                               os.environ.get("VENTORYN_SHOT_TOKEN", ""))
    return _capture_local(url, out_path, timeout_ms)


def _capture_remote(url: str, out_path: Path, service: str, token: str,
                    timeout: float = 45) -> tuple[bool, str]:
    """Ask the screenshot container for a PNG. The service address must be
    on this machine (loopback) — it is configuration, not user input."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    host = urllib.parse.urlsplit(service).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False, "unavailable — the screenshot service address is not local"
    if not _allowed(url):
        return False, "unavailable — that address is not allowed"
    req = urllib.request.Request(
        service.rstrip("/") + "/shot", data=json.dumps({"url": url}).encode(),
        headers={"Content-Type": "application/json", "X-Shot-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local service
            if resp.headers.get("Content-Type") != "image/png":
                return False, "unavailable — unexpected answer from the screenshot service"
            data = resp.read(10_000_001)
        if len(data) > 10_000_000 or not data.startswith(bytes([0x89]) + b"PNG"):
            return False, "unavailable — unexpected answer from the screenshot service"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        return True, ""
    except urllib.error.HTTPError as exc:
        try:
            note = json.loads(exc.read(2000) or b"{}").get("note")
        except ValueError:
            note = None
        return False, note or f"unavailable — screenshot service answered HTTP {exc.code}"
    except Exception:  # noqa: BLE001 - service down: no screenshot, nothing else breaks
        return False, "unavailable — the screenshot service is not running"


def _capture_local(url: str, out_path: Path, timeout_ms: int = 20_000) -> tuple[bool, str]:
    """The browser itself (sandbox on, every request guarded)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "unavailable — the screenshot browser is not installed on this server"
    if not _allowed(url):
        return False, "unavailable — that address is not allowed"
    with _LOCK:
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(chromium_sandbox=True, args=_ARGS)
                except Exception:  # noqa: BLE001 - sandbox or browser missing
                    return False, ("unavailable — the browser sandbox cannot start "
                                   "on this server, so no screenshot is taken")
                try:
                    ctx = browser.new_context(viewport=VIEWPORT, accept_downloads=False,
                                              service_workers="block",
                                              user_agent="Mozilla/5.0 (compatible; "
                                              "VentorynSnapshot/1.0; +https://ventoryn.app)")
                    page = ctx.new_page()
                    page.route("**/*", lambda route: route.continue_()
                               if _allowed(route.request.url) else route.abort())
                    if hasattr(page, "route_web_socket"):
                        def _ws(ws):
                            if _allowed(ws.url):
                                ws.connect_to_server()
                            else:
                                ws.close()
                        page.route_web_socket("**/*", _ws)
                    page.goto(url, wait_until="load", timeout=timeout_ms)
                    page.wait_for_timeout(1200)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(out_path), type="png")
                    return True, ""
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001 - site slow/broken: no screenshot
            return False, f"unavailable — the page did not finish loading ({type(exc).__name__})"


def snapshot_image_path(reports_dir, site_id: str) -> Path:
    return Path(reports_dir) / "snapshots" / f"{site_id}.png"


def capture_for_site(url: str, site_id: str, reports_dir) -> tuple[str | None, str]:
    """(stored marker or None, note) for site_snapshot.collect."""
    path = snapshot_image_path(reports_dir, site_id)
    ok, note = capture(url, path)
    return ("snapshots/" + path.name if ok else None), note

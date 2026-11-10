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
    """(ok, note). Never raises."""
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

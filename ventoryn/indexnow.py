"""IndexNow: tell Bing (and, through it, Brave Search) about Ventoryn's
own public pages instead of waiting for organic crawling.

Setup: set VENTORYN_INDEXNOW_KEY to any random hex string (e.g.
``python -c "import secrets;print(secrets.token_hex(16))"``) and
redeploy — the verification file is then served automatically at
/<key>.txt. Submit with:

    python -m ventoryn.indexnow

Only Ventoryn's own sitemap URLs are ever submitted — never a
customer's site.
"""

from __future__ import annotations

import json
import urllib.request
from fastapi.responses import PlainTextResponse

INDEXNOW_API_URL = "https://api.indexnow.org/indexnow"


def register_indexnow(app, config) -> None:
    """Serve the key verification file, only when a key is configured."""
    key = config.indexnow_key
    if not key:
        return

    @app.get(f"/{key}.txt", response_class=PlainTextResponse)
    def indexnow_key_file():
        return PlainTextResponse(key)


def submit(config, urls: list[str], urlopen=None) -> tuple[bool, str]:
    """Submit ``urls`` (full URLs, Ventoryn's own) to IndexNow. Returns
    (ok, message) — never raises; a missing key or network failure is
    reported, not fatal."""
    if not config.indexnow_key:
        return False, "VENTORYN_INDEXNOW_KEY is not configured."
    if not urls:
        return False, "No URLs to submit."
    base = (config.base_url or "").rstrip("/")
    host = base.split("://", 1)[-1]
    payload = json.dumps({
        "host": host,
        "key": config.indexnow_key,
        "keyLocation": f"{base}/{config.indexnow_key}.txt",
        "urlList": urls,
    }).encode()
    opener = urlopen or urllib.request.urlopen
    req = urllib.request.Request(
        INDEXNOW_API_URL, data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with opener(req, timeout=10) as resp:
            status = resp.status
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return False, f"IndexNow submission failed: {exc}"
    if status in (200, 202):
        return True, f"Submitted {len(urls)} URL(s) to IndexNow (status {status})."
    return False, f"IndexNow returned status {status}."


if __name__ == "__main__":
    from ventoryn.saas import SaasConfig

    config = SaasConfig.from_env()
    base = (config.base_url or "").rstrip("/")
    ok, message = submit(config, [f"{base}/"])
    print(message)
    raise SystemExit(0 if ok else 1)

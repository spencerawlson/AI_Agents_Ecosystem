"""The screenshot service — runs alone in the `ventoryn-shot` container.

    python -m ventoryn.shot_service [--host 127.0.0.1] [--port 8210]

Why a separate container: opening untrusted websites in a browser is the
riskiest thing Axionory does. Chrome's sandbox needs a more permissive
syscall filter than Docker's default, so the browser runs here instead
of in the app: no database, no secrets file, no reports folder,
read-only file system, all Linux capabilities dropped, memory/CPU/process
limits (see deploy/docker/ventoryn.sh). Even a browser escape would find
nothing worth taking.

API (loopback only, shared token):
    GET  /health                    -> 200 "ok"
    POST /shot  {"url": "https://..."}  header X-Shot-Token
         -> 200 image/png | 403 bad token | 400 bad request
            | 422 {"note": "..."} when no screenshot could be taken
Every request the page makes still goes through core.netguard
(site_screenshot._allowed), and the sandbox is mandatory.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 4096


def make_handler(token: str, capture_fn):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VentorynShot/1.0"
        sys_version = ""

        def log_message(self, fmt, *args):  # never log URLs or tokens
            sys.stderr.write(f"shot: {self.command} {self.path.split('?')[0]} "
                             f"{args[1] if len(args) > 1 else ''}\n")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _note(self, code: int, note: str) -> None:
            self._send(code, json.dumps({"note": note}).encode(), "application/json")

        def do_GET(self):  # noqa: N802 - http.server API
            if self.path == "/health":
                self._send(200, b"ok", "text/plain")
            else:
                self._note(404, "not found")

        def do_POST(self):  # noqa: N802
            if self.path != "/shot":
                return self._note(404, "not found")
            if not hmac.compare_digest(self.headers.get("X-Shot-Token", ""), token):
                return self._note(403, "forbidden")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = MAX_BODY + 1
            if length <= 0 or length > MAX_BODY:
                return self._note(400, "bad request")
            try:
                url = str(json.loads(self.rfile.read(length)).get("url", ""))
            except (ValueError, AttributeError):
                return self._note(400, "bad request")
            if not url.startswith(("http://", "https://")):
                return self._note(400, "bad request")
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "shot.png"
                ok, note = capture_fn(url, out)
                if not ok:
                    return self._note(422, note or "unavailable")
                self._send(200, out.read_bytes(), "image/png")

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Axionory screenshot service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8210)
    args = parser.parse_args(argv)
    token = os.environ.get("VENTORYN_SHOT_TOKEN", "")
    if len(token) < 32:
        print("VENTORYN_SHOT_TOKEN missing or too short — refusing to start",
              file=sys.stderr)
        return 2
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print("the screenshot service only listens on loopback", file=sys.stderr)
        return 2
    from ventoryn.site_screenshot import _capture_local

    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(token, _capture_local))
    print(f"screenshot service on {args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

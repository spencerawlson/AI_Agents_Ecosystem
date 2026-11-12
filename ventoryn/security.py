"""HTTP hardening for the Axionory app.

install(app, config) adds, for every response:

- Security headers: a Content-Security-Policy that only allows this
  origin (pages inline their CSS/JS, so 'unsafe-inline' stays until
  they move to files), no framing (clickjacking), nosniff, a strict
  referrer policy, a permissions policy that turns off camera /
  microphone / geolocation / payment APIs, cross-origin isolation of
  the window, and HSTS when the site is served over HTTPS.
- A cross-site request check: a POST/PUT/PATCH/DELETE whose Origin
  (or, without one, Referer) is another site is refused with 403.
  Session cookies are SameSite=Lax already; this is the second lock.
  The Stripe webhook is exempt — Stripe is a server, not a browser,
  and the webhook is verified by its signature instead.

RateLimiter is a small in-process sliding window used for the public,
unauthenticated endpoints (the free site analysis).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import PlainTextResponse

# Paths a non-browser server POSTs to; verified by signature instead.
# The analytics collector receives page views from customers' own
# sites (cross-site by design); it changes no account state and has
# its own host check, bot filter, size cap and rate limit.
CSRF_EXEMPT_PATHS = ("/account/billing/webhook", "/api/va/collect")

# Where a form submit may lead (POST /account/subscribe redirects to
# Stripe Checkout; Chrome applies form-action to that redirect).
_FORM_TARGETS = ("'self' https://checkout.stripe.com "
                 "https://billing.stripe.com https://accounts.google.com")

CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    f"form-action {_FORM_TARGETS}",
))

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _origin_of(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _allowed_origins(request: Request, base_url: str) -> set[str]:
    allowed = {f"{request.url.scheme}://{request.url.netloc}".lower()}
    host = request.headers.get("host")
    if host:
        # Behind a TLS-terminating proxy the app sees http while the
        # browser's Origin says https.
        allowed |= {f"http://{host}".lower(), f"https://{host}".lower()}
    base = _origin_of(base_url or "")
    if base:
        allowed.add(base)
    return allowed


def is_cross_site(request: Request, base_url: str) -> bool:
    """True when a state-changing request plainly came from another
    site. Requests with neither Origin nor Referer (non-browser
    clients, some privacy setups) are not judged cross-site: browsers
    always send Origin on a cross-site POST, so those are not forged
    form posts."""
    source = request.headers.get("origin")
    if source in (None, ""):
        referer = request.headers.get("referer")
        if not referer:
            return False
        source = _origin_of(referer) or ""
    if source == "null":
        return True
    return source.lower().rstrip("/") not in _allowed_origins(request,
                                                               base_url)


def install(app, config) -> None:
    hsts = config.base_url.lower().startswith("https://")

    @app.middleware("http")
    async def _hardening(request: Request, call_next):
        if (request.method in _UNSAFE_METHODS
                and request.url.path not in CSRF_EXEMPT_PATHS
                and is_cross_site(request, config.base_url)):
            response = PlainTextResponse(
                "Cross-site request refused.", status_code=403)
        else:
            response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if hsts:
            headers.setdefault("Strict-Transport-Security",
                               "max-age=31536000; includeSubDomains")
        return response


class RateLimiter:
    """At most ``limit`` events per ``window`` seconds per key."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._events: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._events.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._events) > 10_000:  # bound memory under abuse
                for k in [k for k, v in self._events.items() if not v][:5000]:
                    self._events.pop(k, None)
            return True

    def retry_after(self, key: str) -> int:
        with self._lock:
            q = self._events.get(key)
            if not q:
                return 0
            return max(1, int(self.window - (time.monotonic() - q[0])) + 1)


def client_key(request: Request) -> str:
    """The rate-limit key: the client IP as the server sees it. Behind
    a reverse proxy, run uvicorn with --proxy-headers (and
    --forwarded-allow-ips) so this is the real client, not the proxy."""
    return request.client.host if request.client else "unknown"

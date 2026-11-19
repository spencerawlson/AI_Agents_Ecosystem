"""Resend email adapter — the only place Axionory talks to Resend.

One pattern, copied from ``ventoryn/social_oauth.py``: all HTTP
through an injected client — an httpx-like object with ``.post``
(a real ``httpx.Client`` is built only when no client is passed),
or a urlopen-style callable for the stdlib path. NOTHING here makes
a live call at import time, and tests use fakes only — no real
Resend HTTP in tests, ever.

Provider facts (endpoints, limits) come from the Phase 1 registry
(``~/workspace/marketing-api-registry/api-permissions.md`` and
``api-rate-limits.md``): ``POST https://api.resend.com/emails``
with ``Authorization: Bearer re_...`` and a JSON body of
``{from, to, subject, html, text}``; a 200 returns ``{"id": ...}``;
the rate limit is 10 requests/second per team and one batch request
covers up to 100 recipients. Resend has no approval queue of its
own — the sender domain must be verified (DKIM + SPF) or Resend
refuses the send.

Refusal is honest: when the operator key is not configured
(``SaasConfig.resend_configured``), every entry point raises
SaasError with a plain-language reason instead of failing or
faking a send. Sending itself is approval-gated one level up, in
``ventoryn/newsletters.py`` — this module only moves bytes.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from ventoryn.saas import SaasError


# ---------------------------------------------------------------------------
# Constant provider URL — never user-supplied (SSRF guard: no fetched
# URL in this module ever comes from customer input).
# ---------------------------------------------------------------------------

RESEND_API_URL = "https://api.resend.com/emails"

#: One batch request covers up to 100 recipients (registry
#: api-rate-limits.md). More than this is refused before any call.
MAX_RECIPIENTS = 100

_HTTP_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# HTTP plumbing — httpx-like client, or a urlopen-style callable.
# (Same shape as ventoryn/social_oauth.py.)
# ---------------------------------------------------------------------------

class _Response:
    """Minimal response shape shared by both client kinds."""

    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _UrlopenClient:
    """Wrap a urlopen-style callable (urllib) as an httpx-like client."""

    def __init__(self, urlopen) -> None:
        self._urlopen = urlopen

    def post(self, url: str, json: dict | None = None,
             headers: dict | None = None) -> _Response:
        body = json.dumps(json or {}).encode("utf-8")
        request = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     **(headers or {})},
            method="POST")
        with self._urlopen(request) as resp:
            return _Response(resp.status,
                             json.loads(resp.read().decode("utf-8")))


def _client_for(client):
    """Normalize the injected client. None builds a real httpx client
    (explicit timeout, constant URL only); a callable is treated as
    a urlopen-style opener; anything else must already be httpx-like."""
    if client is None:
        import httpx

        return httpx.Client(timeout=_HTTP_TIMEOUT_S)
    if callable(client) and not hasattr(client, "post"):
        return _UrlopenClient(client)
    return client


def _check(response) -> dict:
    """Raise SaasError (plain words, no secret) on a non-2xx response
    or a provider error body; otherwise return the JSON body."""
    status = getattr(response, "status_code", 0)
    try:
        body = response.json()
    except Exception:
        body = {}
    if not (200 <= status < 300) or (isinstance(body, dict)
                                     and body.get("error")):
        detail = ""
        if isinstance(body, dict):
            err = body.get("error") or {}
            if isinstance(err, dict):
                detail = err.get("message") or ""
            elif isinstance(err, str):
                detail = err
        raise SaasError(
            f"Resend did not accept the email. Try again. {detail}".strip())
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send_email(api_key: str, *, from_email: str, to: list[str],
               subject: str, html: str | None = None,
               text: str | None = None, client=None) -> dict:
    """Send one email via Resend. Returns the provider body
    (``{"id": ...}`` on success).

    Raises SaasError — never a fake success — when the key is not
    configured (and makes NO HTTP call then), when the recipient
    list is empty or over the batch cap, or when Resend refuses
    the send. ``to`` is the tenant-supplied recipient list, passed
    through unchanged; this module never invents an address."""
    if not (api_key or "").strip():
        raise SaasError(
            "Email sending is not set up yet — the Resend API key is "
            "not configured.")
    recipients = list(to or [])
    if not recipients:
        raise SaasError("There is no recipient to send this email to.")
    if len(recipients) > MAX_RECIPIENTS:
        raise SaasError(
            f"Too many recipients ({len(recipients)}) — one email "
            f"covers at most {MAX_RECIPIENTS}.")
    if not (from_email or "").strip():
        raise SaasError("There is no From address for this email.")
    if not (subject or "").strip():
        raise SaasError("This email has no subject.")
    http = _client_for(client)
    body: dict = {
        "from": from_email.strip(),
        "to": recipients,
        "subject": subject.strip(),
    }
    if html:
        body["html"] = html
    if text:
        body["text"] = text
    if not html and not text:
        raise SaasError("This email has no content.")
    result = _check(http.post(
        RESEND_API_URL,
        json=body,
        headers={"Authorization": f"Bearer {api_key.strip()}"}))
    if not result.get("id"):
        raise SaasError(
            "Resend did not return an email id. Nothing was recorded "
            "as sent.")
    return result

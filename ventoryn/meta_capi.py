"""Meta Conversions API: Axionory's OWN sign-ups and payments, sent
server-side to Axionory's Meta dataset so Meta ads for Axionory can
optimize for real customers.

Server events only: no Meta Pixel, no browser cookie, so the website
stays cookieless. Personal data is SHA-256 hashed before it leaves the
server (email, account id), as Meta requires.

Never sent:
- when the dataset id or access token is not configured
  (META_CAPI_DATASET_ID / META_CAPI_ACCESS_TOKEN)
- for operator accounts (their own testing is not a customer)
- for an account that opted out (Security, "Ad measurement")

Events:
- CompleteRegistration  first Google sign-in  (action_source=website)
- Subscribe             subscription checkout completed (system_generated)
- Purchase              renewal invoice paid, wallet top-up (system_generated)

event_id is the account id (registration) or the Stripe event id, so a
redelivered webhook is deduplicated by Meta too. META_CAPI_TEST_CODE,
when set, routes events to Events Manager's "Test events" tab.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request

GRAPH = "https://graph.facebook.com/v24.0"


def _env(name: str, env=None) -> str:
    return ((env if env is not None else os.environ).get(name) or "").strip()


def configured(env=None) -> bool:
    return bool(_env("META_CAPI_DATASET_ID", env) and _env("META_CAPI_ACCESS_TOKEN", env))


def _h(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def build_event(name: str, *, event_id: str, account: dict, action_source: str,
                value_cents: int | None = None, currency: str = "usd",
                ip: str = "", user_agent: str = "", source_url: str = "") -> dict:
    user = {"external_id": [_h(account["id"])]}
    if account.get("email"):
        user["em"] = [_h(account["email"])]
    if ip:
        user["client_ip_address"] = ip
    if user_agent:
        user["client_user_agent"] = user_agent
    event = {"event_name": name, "event_time": int(time.time()), "event_id": event_id,
             "action_source": action_source, "user_data": user}
    if source_url:
        event["event_source_url"] = source_url
    if value_cents is not None:
        event["custom_data"] = {"value": round(value_cents / 100, 2),
                                "currency": (currency or "usd").upper()}
    return event


def send(events: list[dict], env=None, urlopen=None) -> dict:
    body = {"data": events, "access_token": _env("META_CAPI_ACCESS_TOKEN", env)}
    if _env("META_CAPI_TEST_CODE", env):
        body["test_event_code"] = _env("META_CAPI_TEST_CODE", env)
    req = urllib.request.Request(
        f"{GRAPH}/{_env('META_CAPI_DATASET_ID', env)}/events",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
        method="POST")
    with (urlopen or urllib.request.urlopen)(req, timeout=20) as resp:
        return json.loads(resp.read() or b"{}")


def _send_quietly(event: dict) -> None:
    try:
        out = send([event])
        print(f"meta capi: {event['event_name']} sent "
              f"(received {out.get('events_received', '?')})", flush=True)
    except Exception as exc:  # noqa: BLE001 - measurement never breaks a request
        print(f"meta capi: {event['event_name']} not sent: {type(exc).__name__}", flush=True)


def _dispatch(event: dict) -> None:
    """Send in the background. Tests replace this."""
    threading.Thread(target=_send_quietly, args=(event,), daemon=True).start()


def track(store, config, account: dict | None, name: str, *, event_id: str,
          action_source: str = "system_generated", value_cents: int | None = None,
          currency: str = "usd", ip: str = "", user_agent: str = "") -> bool:
    """Queue one event when allowed. Returns True when queued."""
    if account is None or not configured():
        return False
    if config.is_operator(account.get("email") or ""):
        return False
    fresh = store.get_account(account["id"]) or account
    if int(fresh.get("ad_measurement_opt_out") or 0):
        return False
    _dispatch(build_event(name, event_id=event_id, account=fresh, action_source=action_source,
                          value_cents=value_cents, currency=currency, ip=ip,
                          user_agent=user_agent,
                          source_url=config.base_url.rstrip("/") + "/"))
    return True

"""Meta data deletion + deauthorize callbacks.

Meta requires a Data Deletion Callback URL for apps that use Facebook
Login. When a person removes Axionory in their Facebook settings (or asks
for their data to be deleted), Meta POSTs a ``signed_request`` to us:

    signed_request = base64url(HMAC-SHA256(payload, app_secret)) "." base64url(payload)
    payload        = {"algorithm": "HMAC-SHA256", "user_id": "<app-scoped id>", "issued_at": ...}

We verify the signature with the app secret, delete every Facebook Page
and Instagram connection that person made (tokens included), and answer
with a status URL + confirmation code, as Meta asks:

    {"url": "https://www.axionory.com/meta/data-deletion/status?code=...",
     "confirmation_code": "..."}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

META_KINDS = ("facebook", "instagram")


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def parse_signed_request(signed_request: str, app_secret: str) -> dict | None:
    """The verified payload, or None when it is malformed or forged."""
    if not signed_request or not app_secret or "." not in signed_request:
        return None
    sig_part, payload_part = signed_request.split(".", 1)
    try:
        sig = _b64url(sig_part)
        payload = json.loads(_b64url(payload_part))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict) or str(payload.get("algorithm", "")).upper() != "HMAC-SHA256":
        return None
    good = hmac.new(app_secret.encode(), payload_part.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(good, sig):
        return None
    return payload


def delete_for_user(store, fb_user_id: str) -> int:
    """Disconnect (tokens deleted) every Facebook/Instagram connection made
    by this Facebook user. Returns how many were removed."""
    if not fb_user_id:
        return 0
    removed = 0
    for kind in META_KINDS:
        for site_id in store.site_ids_with_connection(kind):
            raw = (store.get_connection(site_id, kind) or {}).get("_detail_raw") or {}
            if raw.get("fb_user_id") == fb_user_id:
                store.disconnect_connection(site_id, kind)
                removed += 1
    return removed

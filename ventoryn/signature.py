"""The Ventoryn signature — how agent work is followed to checkout.

Every agent output that contains a link (post, reply, article,
recommendation) carries the signature:

* UTM parameters: ``utm_source=ventoryn``, ``utm_medium=<agent>``,
  ``utm_campaign=<site>``
* a signed per-action tag ``vtn_<id>`` (``utm_content``), generated
  per site + per agent action and RECORDED when the link is created
  (what it points to, which agent, which job, when)

On the revenue side a payment is attributed to Ventoryn work ONLY
where the signature reaches the payment (the tag in Stripe metadata
/ the webhook match). A payment without it is reported as
unattributed revenue — never assigned to a post by guesswork.

The tag is signed with HMAC-SHA256 under the same
``VENTORYN_TOKEN_KEY`` as the token encryption (see
``ventoryn/token_crypto.py``), so a tag cannot be forged by editing a
URL — the server can verify it. The tag itself carries no secret.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ventoryn.token_crypto import _derive_key

UTM_SOURCE = "ventoryn"
_TAG_PREFIX = "vtn"


def make_tag(site_id: int, agent: str, action_id: str,
             key: str | bytes | None = None) -> str:
    """A signed per-action tag: ``vtn_<action_id>.<short-mac>``.
    `action_id` is the caller's stable id for the action (job id +
    sequence, draft id, …). The MAC binds the tag to the site and
    the agent, so a tag copied onto another site's link fails
    verification."""
    mac = _hmac.new(
        _derive_key(key),
        f"{site_id}:{agent}:{action_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12]
    return f"{_TAG_PREFIX}_{action_id}.{mac}"


def verify_tag(tag: str, site_id: int, agent: str, action_id: str,
               key: str | bytes | None = None) -> bool:
    """True when `tag` is the tag :func:`make_tag` made for this
    site + agent + action."""
    if not isinstance(tag, str):
        return False
    try:
        return _hmac.compare_digest(tag, make_tag(site_id, agent, action_id, key))
    except Exception:
        return False


def parse_tag(tag: str) -> tuple[str, str] | None:
    """Split a ``vtn_<action_id>.<mac>`` tag into (action_id, mac)."""
    if not isinstance(tag, str) or not tag.startswith(_TAG_PREFIX + "_"):
        return None
    rest = tag[len(_TAG_PREFIX) + 1:]
    if "." not in rest:
        return None
    action_id, mac = rest.rsplit(".", 1)
    if not action_id or not mac:
        return None
    return action_id, mac


def sign_url(url: str, site_slug: str, agent: str, tag: str) -> str:
    """Add the Ventoryn signature to a link: UTM parameters plus
    the signed tag as ``utm_content``. Existing query parameters
    are kept; a pre-existing utm_* is overwritten (the signature
    is authoritative)."""
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["utm_source"] = UTM_SOURCE
    params["utm_medium"] = agent
    params["utm_campaign"] = site_slug
    params["utm_content"] = tag
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urlencode(params), parts.fragment,
    ))


def tag_from_url(url: str) -> str | None:
    """The ``utm_content`` tag carried by a signed URL, if any."""
    try:
        params = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    except Exception:
        return None
    tag = params.get("utm_content")
    return tag if tag and tag.startswith(_TAG_PREFIX + "_") else None


def new_action_id() -> str:
    """A fresh stable action id (time-ordered, unguessable)."""
    return f"{int(time.time())}-{secrets.token_hex(4)}"


def sign_agent_link(url: str, site_id: str, site_slug: str, agent: str,
                    job_id: str, store, key: str | bytes | None = None
                    ) -> tuple[str, str]:
    """Sign one agent link and RECORD it: create the per-action tag,
    add the UTM parameters + tag to the URL, and store the record
    (what it points to, which agent, which job, when) in the store.
    Returns (signed_url, tag). This is the one entry point every
    agent output uses — a link that was not created here has no
    record, so a payment carrying its tag can never be attributed.
    `store` is a ventoryn.saas.SaasStore (duck-typed: record_signature)."""
    action_id = new_action_id()
    tag = make_tag(site_id, agent, action_id, key=key)
    signed = sign_url(url, site_slug, agent, tag)
    store.record_signature(site_id, tag, action_id, agent, job_id, signed)
    return signed, tag

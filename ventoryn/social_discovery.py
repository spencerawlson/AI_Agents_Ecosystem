"""Permitted READ-ONLY discovery on Facebook and Instagram.

This module only READS. There is no write method here at all — no
replies, no unsolicited mentions, no DMs, no follows, no likes.
Discovery output is a list of candidates for a HUMAN to turn into
a draft (``SaasStore.create_social_draft`` on main); nothing
found here is ever auto-posted.

Every function takes an injected read-only client (httpx-like with
``.get``, or a urlopen-style callable) and an access token the caller
already holds. No live call is made at import time; tests use fakes.

Candidate shape (normalized across providers):
{"provider", "id", "text", "url", "author", "created_at"}.

X is deliberately not here: X discovery/search is not part of
this port — X on main has no discovery module.
"""


from __future__ import annotations

from ventoryn.saas import SaasError
from ventoryn.social_oauth import _check, _client_for


GRAPH_BASE = "https://graph.facebook.com/v24.0"


def _candidates(provider: str, items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        out.append({
            "provider": provider,
            "id": str(item.get("id") or ""),
            "text": item.get("text") or "",
            "url": item.get("url") or "",
            "author": item.get("author") or "",
            "created_at": item.get("created_at") or "",
        })
    return out


def read_facebook_page_feed(client, page_id: str, page_access_token: str,
                            limit: int = 10) -> list[dict]:
    """Facebook Page feed read (GET /{page-id}/feed). Read-only —
    the customer's own Page posts, for turning into drafts."""
    if not page_id:
        raise SaasError("Give a Facebook Page id to read.")
    http = _client_for(client)
    body = _check(http.get(
        f"{GRAPH_BASE}/{page_id}/feed",
        params={"fields": "id,message,created_time,permalink_url,from",
                "limit": max(1, min(int(limit), 100)),
                "access_token": page_access_token}),
        "facebook", "Page feed read")
    items = []
    for post in body.get("data") or []:
        author = post.get("from") or {}
        items.append({
            "id": post.get("id"),
            "text": post.get("message"),
            "url": post.get("permalink_url"),
            "author": author.get("name") or "",
            "created_at": post.get("created_time"),
        })
    return _candidates("facebook", items)


def read_instagram_media(client, ig_user_id: str, access_token: str,
                         limit: int = 10) -> list[dict]:
    """Instagram media read (GET /{ig-user-id}/media). Read-only —
    the account's own media, for turning into drafts."""
    if not ig_user_id:
        raise SaasError("Give an Instagram user id to read.")
    http = _client_for(client)
    body = _check(http.get(
        f"{GRAPH_BASE}/{ig_user_id}/media",
        params={"fields": "id,caption,timestamp,permalink",
                "limit": max(1, min(int(limit), 100)),
                "access_token": access_token}),
        "instagram", "media read")
    items = []
    for media in body.get("data") or []:
        items.append({
            "id": media.get("id"),
            "text": media.get("caption"),
            "url": media.get("permalink"),
            "author": "",
            "created_at": media.get("timestamp"),
        })
    return _candidates("instagram", items)

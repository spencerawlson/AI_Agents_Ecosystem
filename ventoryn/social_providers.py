"""Native publishing clients for Facebook and Instagram.

One small client per provider, each with a single write method:
``publish_post(text, link, ...) -> {"provider_post_id": ..., "response": ...}``.

Rules these clients follow (do not weaken):

* Constant provider host URLs only — never a user-supplied URL.
* httpx with an explicit timeout; an injected client (httpx-like
  with ``.get`` / ``.post``) is used when one is passed, so tests
  never construct a real client and never make a live call.
* A clean provider error (an HTTP error response) raises
  :class:`ProviderError` — the publish is FAILED, with the error
  stored. A timeout or transport failure with NO response raises
  :class:`ProviderAmbiguousError` — the post may or may not have
  landed, so it is NEVER auto-retried as a new post: the draft
  is left undecided and the ambiguity is reported to the
  operator instead of silently re-posting.
* No provider here offers an idempotency key/header on these
  endpoints, so the server-side guard is main's draft system
  (``ventoyrn/saas.py``: ``decide_social_draft`` only updates a
  draft still ``pending``, and approve-and-post decides the
  draft ``posted`` in the same flow) — a second approve call
  for the same draft returns the already-decided outcome
  without calling the provider again.

Instagram constraint (code + docstring + the draft layer enforces
it): caption URLs are PLAIN TEXT, not clickable — the caption
carries the plain-text URL, and the customer-facing copy must say
the link is not tappable. Attribution for Instagram is the
plain-text URL plus link-in-bio / a Story sticker, never a promised
click.

X is deliberately not here: X publishing stays inline on main
(``ventoyrn/saas.py``: ``x_post`` / ``post_to_x``).
"""


from __future__ import annotations

import httpx

from ventoryn.social_oauth import _client_for


_HTTP_TIMEOUT_S = 30.0

GRAPH_BASE = "https://graph.facebook.com/v24.0"


class ProviderError(Exception):
    """The provider answered with an error — a clean failure. The
    publish is FAILED; the error is stored on the draft."""


class ProviderAmbiguousError(Exception):
    """No provider response (timeout / transport failure). The post
    may or may not have landed — reconcile by hand, never re-post."""


def _post(http, url: str, payload: dict, headers: dict,
          provider: str) -> dict:
    """POST and return the JSON body, mapping failures to the two
    error kinds above. A response object with a non-2xx status is a
    clean ProviderError; a timeout/transport exception with no
    response is ambiguous."""
    try:
        response = http.post(url, json=payload, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise ProviderAmbiguousError(
            f"{provider}: no response from the provider ({exc!r}) — "
            "the post may or may not have been published.") from exc
    except Exception as exc:  # a fake client raising a timeout-like error
        if exc.__class__.__name__ in ("TimeoutError", "Timeout"):
            raise ProviderAmbiguousError(
                f"{provider}: no response from the provider ({exc!r}) — "
                "the post may or may not have been published.") from exc
        raise
    status = getattr(response, "status_code", 0)
    try:
        body = response.json()
    except Exception:
        body = {}
    if not (200 <= status < 300):
        detail = ""
        if isinstance(body, dict):
            err = body.get("error") or {}
            if isinstance(err, dict):
                detail = (err.get("message") or err.get("error_description")
                          or str(err.get("code") or ""))
            elif isinstance(err, str):
                detail = err
        raise ProviderError(
            f"{provider} refused the post (HTTP {status})"
            + (f": {detail}" if detail else ""))
    return body if isinstance(body, dict) else {}


class FacebookClient:
    """POST /{page-id}/feed with {"message": text, "link": link}."""

    provider = "facebook"

    def __init__(self, page_id: str, page_access_token: str,
                 client=None) -> None:
        self.page_id = page_id
        self.page_access_token = page_access_token
        self._http = _client_for(client)

    def publish_post(self, text: str, link: str = "",
                     idempotency_key: str | None = None) -> dict:
        # Facebook offers no idempotency key on POST /{page-id}/feed;
        # the dedupe is main's draft system: decide_social_draft
        # only updates a draft still pending.
        body = _post(self._http, f"{GRAPH_BASE}/{self.page_id}/feed",
                     {"message": text, "link": link,
                      "access_token": self.page_access_token},
                     {}, "facebook")
        post_id = body.get("id") or ""
        if not post_id:
            raise ProviderError("Facebook did not return a post id.")
        return {"provider_post_id": post_id, "response": body}


class InstagramClient:
    """Two-step container flow:

    1. POST /{ig-user-id}/media {"caption": text-with-plain-text-URL,
       "link": ...} (+ "image_url" when the draft carries media)
       -> creation id.
    2. POST /{ig-user-id}/media_publish {"creation_id": ...}
       -> the published media id.

    IG CONSTRAINT: caption URLs are plain text, NOT clickable. The
    caption MUST carry the plain-text URL (the draft layer appends
    it), and the customer-facing copy MUST say the link is not
    tappable. Attribution for Instagram is the plain-text URL plus
    link-in-bio / a Story sticker — never a promised click."""

    provider = "instagram"

    def __init__(self, ig_user_id: str, access_token: str,
                 client=None) -> None:
        self.ig_user_id = ig_user_id
        self.access_token = access_token
        self._http = _client_for(client)

    def publish_post(self, text: str, link: str = "",
                     idempotency_key: str | None = None,
                     image_url: str | None = None) -> dict:
        # Instagram offers no idempotency key on the container flow;
        # the dedupe is main's draft system: decide_social_draft
        # only updates a draft still pending.
        caption = text if not link else f"{text}\n{link}"
        container: dict = {
            "caption": caption,
            "link": link,
            "access_token": self.access_token,
        }
        if image_url:
            container["image_url"] = image_url
        created = _post(self._http,
                        f"{GRAPH_BASE}/{self.ig_user_id}/media",
                        container, {}, "instagram")
        creation_id = created.get("id") or ""
        if not creation_id:
            raise ProviderError(
                "Instagram did not return a media container id.")
        published = _post(
            self._http, f"{GRAPH_BASE}/{self.ig_user_id}/media_publish",
            {"creation_id": creation_id,
             "access_token": self.access_token}, {}, "instagram")
        post_id = published.get("id") or ""
        if not post_id:
            raise ProviderError(
                "Instagram did not return a published media id.")
        return {"provider_post_id": post_id,
                "response": {"container": created, "publish": published}}

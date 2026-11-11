"""Fake-client unit tests for the Facebook/Instagram publishing
clients (ventoryn/social_providers.py) and the read-only discovery
readers (ventoryn/social_discovery.py).

No live provider calls anywhere: every test injects a fake
httpx-like client. These cover the two modules the wiring tests do
not reach — the publish_post contract (URL, payload, post id,
clean error vs ambiguous timeout) and the discovery normalization.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from ventoryn.saas import SaasError
from ventoryn.social_discovery import (
    read_facebook_page_feed,
    read_instagram_media,
)
from ventoryn.social_providers import (
    FacebookClient,
    InstagramClient,
    ProviderAmbiguousError,
    ProviderError,
)


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class _FakeHttp:
    """httpx-like fake: post outcomes are popped in order (a _Resp
    or an exception to raise); get always returns one response."""

    def __init__(self, post_outcomes=None, get_response=None):
        self.post_outcomes = list(post_outcomes or [])
        self.get_response = get_response or _Resp(200, {"data": []})
        self.posts = []
        self.gets = []

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json, headers))
        outcome = self.post_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get(self, url, params=None):
        self.gets.append((url, params))
        return self.get_response


# ---------------------------------------------------------------------------
# FacebookClient
# ---------------------------------------------------------------------------

def test_facebook_publish_post_success():
    http = _FakeHttp([_Resp(200, {"id": "fb-post-1"})])
    client = FacebookClient("page-1", "page-token-1", client=http)
    result = client.publish_post("Hello page",
                                 link="https://ada.example.com/welcome")
    assert result["provider_post_id"] == "fb-post-1"
    assert len(http.posts) == 1
    url, payload, _headers = http.posts[0]
    assert url == "https://graph.facebook.com/v24.0/page-1/feed"
    assert payload["message"] == "Hello page"
    assert payload["link"] == "https://ada.example.com/welcome"
    assert payload["access_token"] == "page-token-1"


def test_facebook_publish_post_provider_error_is_clean():
    http = _FakeHttp([_Resp(400, {"error": {
        "message": "Invalid OAuth access token", "code": 190}})])
    client = FacebookClient("page-1", "page-token-1", client=http)
    with pytest.raises(ProviderError) as excinfo:
        client.publish_post("Hello page")
    assert "refused the post (HTTP 400)" in str(excinfo.value)
    assert "Invalid OAuth access token" in str(excinfo.value)


def test_facebook_publish_post_without_post_id_is_clean_error():
    http = _FakeHttp([_Resp(200, {})])
    client = FacebookClient("page-1", "page-token-1", client=http)
    with pytest.raises(ProviderError) as excinfo:
        client.publish_post("Hello page")
    assert "did not return a post id" in str(excinfo.value)


def test_facebook_publish_post_timeout_is_ambiguous():
    http = _FakeHttp([httpx.TimeoutException("timed out")])
    client = FacebookClient("page-1", "page-token-1", client=http)
    with pytest.raises(ProviderAmbiguousError) as excinfo:
        client.publish_post("Hello page")
    assert "may or may not have been published" in str(excinfo.value)


# ---------------------------------------------------------------------------
# InstagramClient
# ---------------------------------------------------------------------------

def test_instagram_publish_post_two_step_success():
    http = _FakeHttp([
        _Resp(200, {"id": "container-1"}),
        _Resp(200, {"id": "ig-media-1"}),
    ])
    client = InstagramClient("ig-99", "ig-token-1", client=http)
    result = client.publish_post(
        "New drop", link="https://ada.example.com/welcome")
    assert result["provider_post_id"] == "ig-media-1"
    assert len(http.posts) == 2
    create_url, container, _ = http.posts[0]
    assert create_url == "https://graph.facebook.com/v24.0/ig-99/media"
    # The caption carries the plain-text URL, appended exactly once.
    assert container["caption"] == (
        "New drop\nhttps://ada.example.com/welcome")
    assert container["link"] == "https://ada.example.com/welcome"
    assert container["access_token"] == "ig-token-1"
    publish_url, publish_payload, _ = http.posts[1]
    assert publish_url == (
        "https://graph.facebook.com/v24.0/ig-99/media_publish")
    assert publish_payload["creation_id"] == "container-1"


def test_instagram_publish_post_without_link_caption_is_plain_text():
    http = _FakeHttp([
        _Resp(200, {"id": "container-1"}),
        _Resp(200, {"id": "ig-media-1"}),
    ])
    client = InstagramClient("ig-99", "ig-token-1", client=http)
    client.publish_post("Just a photo, no link")
    _url, container, _headers = http.posts[0]
    assert container["caption"] == "Just a photo, no link"
    assert container["link"] == ""


def test_instagram_publish_post_container_error_never_publishes():
    http = _FakeHttp([_Resp(400, {"error": {
        "message": "Media container failed", "code": 9004}})])
    client = InstagramClient("ig-99", "ig-token-1", client=http)
    with pytest.raises(ProviderError) as excinfo:
        client.publish_post("New drop")
    assert "refused the post (HTTP 400)" in str(excinfo.value)
    assert len(http.posts) == 1  # media_publish never called


def test_instagram_publish_post_timeout_on_publish_step_is_ambiguous():
    http = _FakeHttp([
        _Resp(200, {"id": "container-1"}),
        httpx.TimeoutException("timed out"),
    ])
    client = InstagramClient("ig-99", "ig-token-1", client=http)
    with pytest.raises(ProviderAmbiguousError) as excinfo:
        client.publish_post("New drop")
    assert "may or may not have been published" in str(excinfo.value)
    assert len(http.posts) == 2


def test_instagram_publish_post_without_container_id_is_clean_error():
    http = _FakeHttp([_Resp(200, {})])
    client = InstagramClient("ig-99", "ig-token-1", client=http)
    with pytest.raises(ProviderError) as excinfo:
        client.publish_post("New drop")
    assert "did not return a media container id" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Discovery (read-only)
# ---------------------------------------------------------------------------

def test_read_facebook_page_feed_normalizes_candidates():
    http = _FakeHttp(get_response=_Resp(200, {"data": [
        {"id": "post-1", "message": "Sale this week",
         "permalink_url": "https://facebook.com/p/1",
         "from": {"name": "Ada Shop"},
         "created_time": "2026-11-09T10:00:00+0000"},
        {"id": "post-2", "message": "Behind the scenes",
         "permalink_url": "https://facebook.com/p/2",
         "from": {}, "created_time": "2026-11-08T10:00:00+0000"},
    ]}))
    out = read_facebook_page_feed(http, "page-1", "page-token-1")
    assert len(out) == 2
    assert out[0] == {
        "provider": "facebook", "id": "post-1",
        "text": "Sale this week", "url": "https://facebook.com/p/1",
        "author": "Ada Shop", "created_at": "2026-11-09T10:00:00+0000"}
    assert out[1]["author"] == ""
    assert len(http.gets) == 1
    url, params = http.gets[0]
    assert url == "https://graph.facebook.com/v24.0/page-1/feed"
    assert params["access_token"] == "page-token-1"
    assert params["limit"] == 10


def test_read_instagram_media_normalizes_candidates():
    http = _FakeHttp(get_response=_Resp(200, {"data": [
        {"id": "media-1", "caption": "New drop",
         "permalink": "https://instagram.com/p/abc",
         "timestamp": "2026-11-09T10:00:00+0000"},
    ]}))
    out = read_instagram_media(http, "ig-99", "ig-token-1")
    assert out == [{
        "provider": "instagram", "id": "media-1", "text": "New drop",
        "url": "https://instagram.com/p/abc", "author": "",
        "created_at": "2026-11-09T10:00:00+0000"}]
    url, params = http.gets[0]
    assert url == "https://graph.facebook.com/v24.0/ig-99/media"
    assert params["access_token"] == "ig-token-1"


def test_read_facebook_page_feed_empty_page_id_refused():
    with pytest.raises(SaasError):
        read_facebook_page_feed(_FakeHttp(), "", "page-token-1")


def test_read_instagram_media_empty_user_id_refused():
    with pytest.raises(SaasError):
        read_instagram_media(_FakeHttp(), "", "ig-token-1")


def test_read_facebook_page_feed_error_response_raises():
    http = _FakeHttp(get_response=_Resp(400, {"error": {
        "message": "Invalid OAuth access token"}}))
    with pytest.raises(SaasError):
        read_facebook_page_feed(http, "page-1", "bad-token")


def test_read_instagram_media_empty_feed_returns_empty_list():
    http = _FakeHttp(get_response=_Resp(200, {"data": []}))
    assert read_instagram_media(http, "ig-99", "ig-token-1") == []

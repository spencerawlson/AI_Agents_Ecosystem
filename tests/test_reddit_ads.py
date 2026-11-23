"""Reddit Ads adapter (Phase A): paused-by-default, fake transport only.

No live calls: every test uses a RecordingTransport. Reddit's own API
docs are allow-list-gated, so the wire field names here follow the
design doc's cross-verified surface (see core/ads.py); the OpenAPI
spec is the build-time authority before any live write.
"""

import json
import os
import urllib.parse

import pytest

from core.ads import (AdCopy, AdsError, CampaignPlan, LIMITS,
                      MockRedditAdapter, RedditAdsAdapter,
                      configured_ads_adapters)

REDDIT_ENV = ("REDDIT_ADS_CLIENT_ID", "REDDIT_ADS_CLIENT_SECRET",
              "REDDIT_ADS_REFRESH_TOKEN", "REDDIT_ADS_ACCOUNT_ID",
              "REDDIT_ADS_PIXEL_ID")


def _plan(**kw):
    copy = AdCopy(headlines=["Aluminium Laptop Stand", "Shop Now"],
                  descriptions=["Ships with tracking. Order today."],
                  primary_texts=["Aluminium Laptop Stand — tap Shop Now."],
                  keywords=["laptop stand"])
    base = dict(brief_id="laptop-stand", product_title="Aluminium Laptop Stand",
                product_url="https://shop.example/products/stand",
                image_url="https://img.cj/1.jpg", daily_budget_usd=12.5,
                duration_days=7, countries=["US", "CA"], copy=copy,
                max_cpa_usd=9.9)
    base.update(kw)
    return CampaignPlan(**base)


def _adapter(transport, **kw):
    base = dict(client_id="cid", client_secret="csec", refresh_token="rtok",
                account_id="acc1", pixel_id="pix1")
    base.update(kw)
    return RedditAdsAdapter(transport=transport, **base)


class RecordingTransport:
    def __init__(self, responder):
        self.calls = []
        self.responder = responder

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.responder(method, url, body)


def _json(body):
    return json.loads(body.decode()) if body else {}


def _live_responder(counter=None):
    ids = counter if counter is not None else iter(range(100, 200))

    def respond(method, url, body):
        if "access_token" in url:
            return 200, json.dumps({"access_token": "rtok",
                                    "token_type": "bearer", "expires_in": 3600})
        if url.endswith("/reports"):
            return 200, json.dumps({"data": [{
                "spend": 12500000, "impressions": 1000, "clicks": 30,
                "conversions": 2, "conversions_value": 4798}]})
        if "/ad_accounts/" in url and method == "GET":
            return 200, json.dumps({"name": "Shop Ads", "currency": "USD",
                                    "account_status": "ACTIVE"})
        return 200, json.dumps({"id": str(next(ids))})
    return respond


# -- configuration ------------------------------------------------------------

def test_reddit_not_configured_without_env(monkeypatch):
    for k in REDDIT_ENV:
        monkeypatch.delenv(k, raising=False)
    assert RedditAdsAdapter.configured() is False
    assert "reddit" not in configured_ads_adapters()
    with pytest.raises(AdsError):
        RedditAdsAdapter.from_env()


def test_reddit_configured_with_env(monkeypatch):
    for k in REDDIT_ENV:
        monkeypatch.setenv(k, "x")
    assert RedditAdsAdapter.configured() is True
    assert "reddit" in configured_ads_adapters()


# -- create_paused ------------------------------------------------------------

def test_reddit_creates_everything_paused():
    t = RecordingTransport(_live_responder())
    refs = _adapter(t).create_paused(_plan())
    assert refs == {"campaign_id": "100", "ad_group_id": "101", "ad_id": "102"}

    api_calls = [c for c in t.calls if "ads-api.reddit.com" in c[1]]
    assert [c[0] for c in api_calls] == ["POST", "POST", "POST"]
    assert all("Bearer rtok" in c[2].get("Authorization", "") for c in api_calls)
    bodies = [_json(c[3]) for c in api_calls]
    assert [b["configured_status"] for b in bodies] == ["PAUSED"] * 3

    camp = bodies[0]
    assert camp["objective"] == "CONVERSIONS"
    assert "daily_budget_cents" not in camp  # non-CBO: budget lives on the ad group
    assert camp["name"].startswith("Aluminium Laptop Stand")

    group = bodies[1]
    assert group["campaign_id"] == "100"
    assert group["goal_type"] == "DAILY_SPEND"
    assert group["goal_value"] == 12_500_000  # $12.50 in microcurrency
    assert group["conversion_pixel_id"] == "pix1"
    assert group["expand_targeting"] is False
    assert group["targeting"]["geolocations"] == ["US", "CA"]

    ad = bodies[2]
    assert ad["ad_group_id"] == "101"
    assert ad["headline"] == "Aluminium Laptop Stand"
    assert ad["destination_url"] == "https://shop.example/products/stand"
    assert ad["image_url"] == "https://img.cj/1.jpg"


def test_reddit_cbo_objective_puts_budget_on_campaign():
    t = RecordingTransport(_live_responder())
    adapter = _adapter(t, objective="TRAFFIC")
    refs = adapter.create_paused(_plan())
    assert refs["campaign_id"] == "100"
    bodies = [_json(c[3]) for c in t.calls if "ads-api.reddit.com" in c[1]]
    assert bodies[0]["daily_budget_cents"] == 1250
    assert "goal_value" not in bodies[1]  # CBO: the campaign manages the budget


def test_reddit_headline_clipped_to_limit():
    t = RecordingTransport(_live_responder())
    long_copy = AdCopy(headlines=["x" * 400], descriptions=["d"],
                       primary_texts=["p"], keywords=["k"])
    _adapter(t).create_paused(_plan(copy=long_copy))
    bodies = [_json(c[3]) for c in t.calls if "ads-api.reddit.com" in c[1]]
    assert len(bodies[2]["headline"]) == LIMITS["reddit_headline"] == 300


def test_reddit_limits():
    assert LIMITS["reddit_headline"] == 300
    assert LIMITS["reddit_description"] == 180


def test_reddit_create_failure_reports_partial_refs():
    def respond(method, url, body):
        if "access_token" in url:
            return 200, json.dumps({"access_token": "rtok"})
        if "/ad_groups" in url:
            return 400, json.dumps({"error": {"message": "bad pixel"}})
        return 200, json.dumps({"id": "100"})

    t = RecordingTransport(respond)
    with pytest.raises(AdsError) as exc:
        _adapter(t).create_paused(_plan())
    assert "partially created" in str(exc.value)
    assert "100" in str(exc.value)  # the paused campaign ref is reported
    assert "PAUSED" in str(exc.value)
    assert not any(c[1].rstrip("/").endswith("/ads") for c in t.calls)


def test_reddit_unknown_objective_fails_loudly():
    t = RecordingTransport(_live_responder())
    with pytest.raises(AdsError):
        _adapter(t, objective="BOGUS")
    assert t.calls == []


def test_reddit_missing_pixel_refuses_before_any_call():
    t = RecordingTransport(_live_responder())
    with pytest.raises(AdsError) as exc:
        _adapter(t, pixel_id="").create_paused(_plan())
    assert "pixel" in str(exc.value).lower()
    assert t.calls == []  # not even the token exchange ran


# -- activate / pause / budget ------------------------------------------------

def _created():
    t = RecordingTransport(_live_responder())
    refs = _adapter(t).create_paused(_plan())
    t.calls.clear()
    return _adapter(t), t, refs


def test_reddit_activates_children_first():
    adapter, t, refs = _created()
    adapter.activate(refs)
    api = [c for c in t.calls if "ads-api.reddit.com" in c[1]]
    assert [(c[0], c[1].rsplit("/", 1)[-1]) for c in api] == [
        ("PATCH", "102"), ("PATCH", "101"), ("PATCH", "100")]
    assert all(_json(c[3])["configured_status"] == "ACTIVE" for c in api)


def test_reddit_pause():
    adapter, t, refs = _created()
    adapter.pause(refs)
    api = [c for c in t.calls if "ads-api.reddit.com" in c[1]]
    assert len(api) == 1
    assert api[0][0] == "PATCH" and api[0][1].endswith("/campaigns/100")
    assert _json(api[0][3])["configured_status"] == "PAUSED"


def test_reddit_set_daily_budget_targets_ad_group():
    adapter, t, refs = _created()
    adapter.set_daily_budget(refs, 20.0)
    api = [c for c in t.calls if "ads-api.reddit.com" in c[1]]
    assert len(api) == 1
    assert api[0][1].endswith("/ad_groups/101")
    body = _json(api[0][3])
    assert body == {"goal_type": "DAILY_SPEND", "goal_value": 20_000_000}


# -- verify / insights ----------------------------------------------------------

def test_reddit_verify_reads_account():
    t = RecordingTransport(_live_responder())
    out = _adapter(t).verify()
    assert out == {"name": "Shop Ads", "currency": "USD",
                   "account_status": "ACTIVE", "active": True}
    api = [c for c in t.calls if "ads-api.reddit.com" in c[1]]
    assert len(api) == 1 and api[0][0] == "GET"
    assert api[0][1].endswith("/ad_accounts/acc1")


def test_reddit_insights_maps_report_row():
    t = RecordingTransport(_live_responder())
    adapter = _adapter(t)
    refs = adapter.create_paused(_plan())
    ins = adapter.insights(refs)
    assert ins.spend_usd == 12.5 and ins.impressions == 1000
    assert ins.clicks == 30 and ins.purchases == 2
    assert ins.revenue_usd == 47.98


# -- mock adapter ---------------------------------------------------------------

def test_mock_reddit_adapter_paused_lifecycle():
    mock = MockRedditAdapter()
    refs = mock.create_paused(_plan())
    assert set(refs) == {"campaign_id", "ad_group_id", "ad_id"}
    assert mock.campaigns[refs["campaign_id"]]["status"] == "PAUSED"
    assert mock.ad_groups[refs["ad_group_id"]]["goal_value"] == 12_500_000
    mock.activate(refs)
    assert mock.campaigns[refs["campaign_id"]]["status"] == "ACTIVE"
    assert mock.ad_groups[refs["ad_group_id"]]["status"] == "ACTIVE"
    assert mock.ads[refs["ad_id"]]["status"] == "ACTIVE"
    mock.set_daily_budget(refs, 20.0)
    assert mock.ad_groups[refs["ad_group_id"]]["goal_value"] == 20_000_000
    mock.pause(refs)
    assert mock.campaigns[refs["campaign_id"]]["status"] == "PAUSED"
    assert mock.insights(refs).spend_usd == 0.0
    assert mock.verify()["active"] is True

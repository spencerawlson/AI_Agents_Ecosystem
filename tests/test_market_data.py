"""Tests for core.market_data (all network mocked)."""

import sys
import types

import pytest

from core.market_data import (
    EtsyMarketClient,
    MarketDataProvider,
    MarketDataUnavailable,
    TrendsClient,
    clear_cache,
    get_snapshot,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


# -- fakes ---------------------------------------------------------------

class FakeAdapter:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def _request(self, method, path, data=None, files=None, params=None):
        self.calls.append(
            {"method": method, "path": path, "params": params})
        return self.response


class FakeEtsyClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def stats(self, keywords):
        self.calls += 1
        if self.fail:
            raise MarketDataUnavailable("etsy down")
        return {
            "keywords": keywords, "result_count": 1234,
            "avg_price": 12.50, "min_price": 5.00, "max_price": 29.99,
            "currency": "USD", "sample_titles": ["A planner", "B planner"],
        }


class FakeTrendsClient:
    def __init__(self, fail=False, trend="rising"):
        self.fail = fail
        self.trend = trend
        self.calls = 0

    def interest(self, keywords):
        self.calls += 1
        if self.fail:
            raise MarketDataUnavailable("trends down")
        return {k: {"avg_12mo": 40.0, "trend": self.trend, "latest": 62.0}
                for k in keywords}


# -- Etsy client ----------------------------------------------------------

def test_etsy_search_parses_listings():
    resp = {"count": 1234, "results": [
        {"listing_id": 111, "title": "ADHD Planner",
         "price": {"amount": 1299, "divisor": 100, "currency_code": "USD"}},
        {"listing_id": 222, "title": "No price",
         "price": {"amount": 0, "divisor": 100, "currency_code": "USD"}},
    ]}
    client = EtsyMarketClient(FakeAdapter(resp))
    out = client.search("planner", limit=20)
    assert len(out) == 2
    assert out[0] == {
        "listing_id": 111, "title": "ADHD Planner", "price": 12.99,
        "currency": "USD", "url": "https://www.etsy.com/listing/111",
    }
    call = client.adapter.calls[0]
    assert call["path"] == "/listings/active"
    assert call["params"]["keywords"] == "planner"
    assert call["params"]["sort_on"] == "score"


def test_etsy_stats_derives_price_band():
    resp = {"count": 500, "results": [
        {"listing_id": 1, "title": "A",
         "price": {"amount": 800, "divisor": 100, "currency_code": "USD"}},
        {"listing_id": 2, "title": "B",
         "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"}},
    ]}
    stats = EtsyMarketClient(FakeAdapter(resp)).stats("planner")
    assert stats["result_count"] == 500
    assert stats["avg_price"] == 14.0
    assert stats["min_price"] == 8.0
    assert stats["max_price"] == 20.0
    assert stats["sample_titles"] == ["A", "B"]


def test_etsy_401_refreshes_once(monkeypatch, tmp_path):
    import urllib.error
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    calls = {"n": 0}
    resp = {"count": 1, "results": [
        {"listing_id": 9, "title": "P",
         "price": {"amount": 1000, "divisor": 100,
                   "currency_code": "USD"}}]}

    class FlakyAdapter:
        def _request(self, method, path, data=None, files=None, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    path, 401, "Unauthorized", {}, None)
            return resp

    refreshed = {}

    def fake_refresh(keystring, refresh_token):
        refreshed["called"] = True
        return {"access_token": "new-token", "refresh_token": "new-refresh"}

    monkeypatch.setattr("core.etsy_adapter.EtsyAuth.refresh",
                        staticmethod(fake_refresh))

    class FakeAdapterClass:
        def __init__(self, *a, **k):
            pass

        def _request(self, method, path, data=None, files=None, params=None):
            calls["n"] += 1
            return resp

    monkeypatch.setattr("core.etsy_adapter.EtsyCommerceAdapter",
                        FakeAdapterClass)

    creds = {"keystring": "k", "shared_secret": "s", "access_token": "old",
             "refresh_token": "r", "shop_id": "1"}
    client = EtsyMarketClient(FlakyAdapter(), creds=creds)
    out = client.search("planner")
    assert refreshed.get("called") is True
    assert out[0]["listing_id"] == 9
    assert client.creds["access_token"] == "new-token"
    assert calls["n"] == 2


def test_etsy_401_without_creds_raises():
    import urllib.error

    class Always401:
        def _request(self, *a, **k):
            raise urllib.error.HTTPError("/x", 401, "Unauthorized", {}, None)

    with pytest.raises(MarketDataUnavailable):
        EtsyMarketClient(Always401()).search("planner")


# -- Trends client (pytrends mocked) ---------------------------------------

def _mock_pytrends(monkeypatch, values, explore_widgets="default"):
    import json

    import pandas as pd
    import requests

    dates = pd.date_range("2025-10-01", periods=len(values), freq="W")
    df = pd.DataFrame({"planner": values}, index=dates)

    class FakeTrendReq:
        # _build_payload_via_get reads pt.headers for the explore GET.
        headers = {"accept-language": "en-US"}

        def __init__(self, *a, **k):
            pass

        def interest_over_time(self):
            return df

    mod = types.ModuleType("pytrends.request")
    mod.TrendReq = FakeTrendReq
    monkeypatch.setitem(sys.modules, "pytrends.request", mod)

    # Mock the GET explore call (pytrends' POST explore is dead: HTTP 411).
    if explore_widgets == "default":
        widgets = [{"id": "TIMESERIES", "token": "tok",
                    "request": {"q": "planner"}}]
    else:
        widgets = explore_widgets
    body = ")]}'\n" + json.dumps({"widgets": widgets})

    class FakeResp:
        status_code = 200
        text = body

    class FakeSession:
        headers = {}

        def get(self, url, params=None, timeout=None):
            assert "trends.google.com/trends/api/explore" in url
            return FakeResp()

    monkeypatch.setattr(requests, "session", lambda: FakeSession())


def test_trends_rising(monkeypatch):
    _mock_pytrends(monkeypatch, [10] * 6 + [50] * 6)
    out = TrendsClient().interest(["planner"])
    assert out["planner"]["trend"] == "rising"


def test_trends_falling(monkeypatch):
    _mock_pytrends(monkeypatch, [50] * 6 + [10] * 6)
    out = TrendsClient().interest(["planner"])
    assert out["planner"]["trend"] == "falling"


def test_trends_flat(monkeypatch):
    _mock_pytrends(monkeypatch, [30] * 12)
    out = TrendsClient().interest(["planner"])
    assert out["planner"]["trend"] == "flat"
    assert out["planner"]["avg_12mo"] == 30.0


def test_trends_failure_becomes_unavailable(monkeypatch):
    mod = types.ModuleType("pytrends.request")

    class Boom:
        def __init__(self, *a, **k):
            raise OSError("429 too many requests")

    mod.TrendReq = Boom
    monkeypatch.setitem(sys.modules, "pytrends.request", mod)
    with pytest.raises(MarketDataUnavailable):
        TrendsClient().interest(["planner"])


def test_trends_explore_uses_get_and_sets_timeseries_widget(monkeypatch):
    import json

    import requests

    seen = {}

    class FakeResp:
        status_code = 200
        text = ")]}'\n" + json.dumps(
            {"widgets": [{"id": "TIMESERIES", "token": "tok123",
                          "request": {"q": "x"}}]})

    class FakeSession:
        headers = {"accept-language": "en-US"}

        def get(self, url, params=None, timeout=None):
            seen["url"] = url
            seen["params"] = params
            return FakeResp()

    monkeypatch.setattr(requests, "session", lambda: FakeSession())

    class FakeTrendReq:
        headers = {"accept-language": "en-US"}

        def __init__(self, *a, **k):
            pass

    pt = FakeTrendReq()
    TrendsClient()._build_payload_via_get(pt, ["cissp"])
    assert "trends.google.com/trends/api/explore" in seen["url"]
    assert pt.interest_over_time_widget["token"] == "tok123"
    # keywords are embedded in the req payload, geo-scoped
    req = json.loads(seen["params"]["req"])
    assert req["comparisonItem"][0]["keyword"] == "cissp"
    assert req["comparisonItem"][0]["geo"] == "US"


def test_trends_explore_429_reports_rate_limit(monkeypatch):
    import requests

    class FakeResp:
        status_code = 429
        text = ""

    class FakeSession:
        headers = {}

        def get(self, url, params=None, timeout=None):
            return FakeResp()

    monkeypatch.setattr(requests, "session", lambda: FakeSession())

    class FakeTrendReq:
        headers = {}

        def __init__(self, *a, **k):
            pass

    with pytest.raises(MarketDataUnavailable, match="rate-limited"):
        TrendsClient()._build_payload_via_get(FakeTrendReq(), ["cissp"])


def test_trends_explore_without_timeseries_widget(monkeypatch):
    _mock_pytrends(monkeypatch, [30] * 12, explore_widgets=[{"id": "GEO_MAP"}])
    with pytest.raises(MarketDataUnavailable, match="no TIMESERIES"):
        TrendsClient().interest(["planner"])


# -- snapshot cache + partial failure --------------------------------------

def test_snapshot_ttl_cache_avoids_new_calls():
    etsy, trends = FakeEtsyClient(), FakeTrendsClient()
    s1 = get_snapshot(["planner"], etsy_client=etsy, trends_client=trends)
    s2 = get_snapshot(["planner"], etsy_client=etsy, trends_client=trends)
    assert s1 is s2
    assert etsy.calls == 1 and trends.calls == 1


def test_snapshot_partial_when_one_source_fails():
    snap = get_snapshot(["planner"],
                        etsy_client=FakeEtsyClient(fail=True),
                        trends_client=FakeTrendsClient())
    assert snap.partial is True
    assert snap.etsy_stats == {}
    assert snap.trends["planner"]["trend"] == "rising"


def test_snapshot_raises_when_both_fail():
    with pytest.raises(MarketDataUnavailable):
        get_snapshot(["planner"],
                     etsy_client=FakeEtsyClient(fail=True),
                     trends_client=FakeTrendsClient(fail=True))


def test_snapshot_raises_with_no_clients():
    with pytest.raises(MarketDataUnavailable):
        get_snapshot(["planner"])


# -- market block ----------------------------------------------------------

def test_market_block_contains_real_numbers():
    provider = MarketDataProvider(etsy_client=FakeEtsyClient(),
                                  trends_client=FakeTrendsClient())
    block = provider.market_block(["planner"])
    assert "LIVE MARKET DATA" in block
    assert "1234" in block  # real result count
    assert "12.50" in block  # real avg price
    assert "rising" in block  # real trend direction


def test_market_block_empty_when_unavailable():
    provider = MarketDataProvider(
        etsy_client=FakeEtsyClient(fail=True),
        trends_client=FakeTrendsClient(fail=True))
    assert provider.market_block(["planner"]) == ""


# -- source prompt integration ----------------------------------------------

class FakeGateway:
    def __init__(self, payload=None, fail=False):
        self.payload = payload or {}
        self.fail = fail
        self.prompts = []

    def complete(self, prompt, tier=None, system=None, json_mode=True):
        from core.llm import LLMUnavailable

        self.prompts.append(prompt)
        if self.fail:
            raise LLMUnavailable("no key")
        return {"text": "{}", "json": self.payload, "model": "fake",
                "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0001}


class StubMarket:
    def __init__(self, block=""):
        self._block = block
        self.watchlist = ["planner"]

    def market_block(self, keywords):
        return self._block


def _opp_payload():
    return {"opportunities": [{
        "niche": "printable planner", "business_type": "digital_product",
        "demand_score": 0.8, "competition_score": 0.4,
        "expected_margin": 0.9, "startup_cost_usd": 50,
        "advertising_cost_usd": 20, "operational_complexity": 0.2,
        "automation_potential": 0.8, "scalability": 0.7,
        "recurring_revenue": 0.3, "marketplace_risk": 0.4,
        "supplier_risk": 0.1, "support_burden": 0.2,
        "time_to_market_days": 14,
    }]}


def test_discovery_prompt_includes_market_block():
    from agents.discovery.llm_source import LLMOpportunitySource

    gw = FakeGateway(payload=_opp_payload())
    src = LLMOpportunitySource(
        gateway=gw, market=StubMarket("LIVE MARKET DATA\n- planner: rising"))
    out = src.fetch({"limit": 1})
    assert len(out) == 1
    assert "LIVE MARKET DATA" in gw.prompts[0]
    assert "rising" in gw.prompts[0]


def test_discovery_prompt_clean_without_market():
    from agents.discovery.llm_source import LLMOpportunitySource

    gw = FakeGateway(payload=_opp_payload())
    src = LLMOpportunitySource(gateway=gw, market=None)
    out = src.fetch({"limit": 1})
    assert len(out) == 1
    assert "LIVE MARKET DATA" not in gw.prompts[0]
    # empty block also omits cleanly
    gw2 = FakeGateway(payload=_opp_payload())
    src2 = LLMOpportunitySource(gateway=gw2, market=StubMarket(""))
    src2.fetch({"limit": 1})
    assert "LIVE MARKET DATA" not in gw2.prompts[0]


def test_research_prompt_includes_market_block():
    from agents.research.llm_source import LLMResearchSource

    gw = FakeGateway(payload={"verdict": "pursue"})
    src = LLMResearchSource(
        gateway=gw,
        market=StubMarket("LIVE MARKET DATA\n- printable planner: "
                          "Etsy: ~1234 active listings"))
    out = src.research({"id": "o1", "niche": "printable planner"})
    assert out["verdict"] == "pursue"
    assert "LIVE MARKET DATA" in gw.prompts[0]
    assert "Do not invent competitor counts" in gw.prompts[0]


def test_sources_fall_back_on_llm_failure():
    from agents.discovery.llm_source import LLMOpportunitySource
    from agents.research.llm_source import LLMResearchSource

    dsrc = LLMOpportunitySource(gateway=FakeGateway(fail=True),
                                market=StubMarket("LIVE MARKET DATA"))
    assert isinstance(dsrc.fetch({"limit": 2}), list)
    assert dsrc.last_usage is None

    class Fallback:
        def research(self, opp):
            return {"verdict": "watch", "niche": opp.get("niche", "")}

    rsrc = LLMResearchSource(gateway=FakeGateway(fail=True),
                             fallback=Fallback(),
                             market=StubMarket("LIVE MARKET DATA"))
    assert rsrc.research({"niche": "x"})["verdict"] == "watch"

"""Real market data for the ecosystem: Etsy marketplace + Google Trends.

Fallback-safe, same pattern as core.llm: MarketDataUnavailable is raised
when live data cannot be fetched, and callers catch it to proceed WITHOUT
the market block — a data failure must NEVER crash a tick.

Sources:
  - Etsy: live active-listing search (result counts + price bands) via the
    shop's existing OAuth token (listings_r scope). No new key needed.
  - Google Trends: 12-month interest direction via pytrends. No key needed.

Rate limiting: get_snapshot() TTL-caches per keyword set (default 1h), so
repeated ticks don't hammer either API.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("ecosystem.market_data")


class MarketDataUnavailable(RuntimeError):
    """Raised when live market data cannot be fetched.

    Callers must catch this and proceed without market data.
    """


# -- Etsy marketplace --------------------------------------------------

def _money(price: dict | None) -> tuple[float, str]:
    if not isinstance(price, dict):
        return 0.0, ""
    amount = price.get("amount", 0) or 0
    divisor = price.get("divisor", 100) or 100
    try:
        return float(amount) / float(divisor), str(price.get("currency_code", ""))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0, ""


class EtsyMarketClient:
    """Live Etsy marketplace search.

    Reuses EtsyCommerceAdapter._request (OAuth bearer token) — the shop's
    existing token doubles as the search key. No separate auth flow.
    Refreshes the OAuth token once on 401, mirroring the experiment
    monitor, so a stale token doesn't kill market data.
    """

    def __init__(self, adapter: Any, creds: dict | None = None) -> None:
        self.adapter = adapter
        self.creds = creds  # {keystring, shared_secret, access_token,
        #                     refresh_token, shop_id} — enables 401 refresh

    def _do_search(self, keywords: str, limit: int) -> dict:
        res = self.adapter._request(
            "GET", "/listings/active",
            params={"keywords": keywords, "limit": limit,
                    "sort_on": "score"})
        return res if isinstance(res, dict) else {}

    def _refresh_once(self) -> None:
        """Refresh OAuth tokens and rebuild the adapter (401 recovery)."""
        import json

        from core.etsy_adapter import EtsyAuth, EtsyCommerceAdapter

        new = EtsyAuth.refresh(self.creds["keystring"],
                               self.creds["refresh_token"])
        self.creds["access_token"] = new["access_token"]
        self.creds["refresh_token"] = new.get("refresh_token",
                                              self.creds["refresh_token"])
        self.adapter = EtsyCommerceAdapter(
            self.creds["keystring"], self.creds["shared_secret"],
            self.creds["access_token"], self.creds["shop_id"])
        try:  # persist so the refresh sticks
            tok_path = Path.home() / ".config" / "evergreen-etsy" / "tokens.json"
            if tok_path.exists():
                tok = json.loads(tok_path.read_text())
                tok["access_token"] = self.creds["access_token"]
                tok["refresh_token"] = self.creds["refresh_token"]
                tok_path.write_text(json.dumps(tok))
                tok_path.chmod(0o600)
        except Exception as exc:
            log.warning("could not persist refreshed Etsy tokens: %s", exc)

    def _raw_search(self, keywords: str, limit: int) -> dict:
        import urllib.error

        try:
            return self._do_search(keywords, limit)
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or not self.creds:
                raise MarketDataUnavailable(
                    f"Etsy marketplace search failed for {keywords!r}: {exc}"
                ) from exc
        log.warning("Etsy token expired, refreshing once")
        try:
            self._refresh_once()
            return self._do_search(keywords, limit)
        except Exception as exc:
            raise MarketDataUnavailable(
                f"Etsy marketplace search failed for {keywords!r} "
                f"(after token refresh): {exc}"
            ) from exc

    def search(self, keywords: str, limit: int = 20) -> list[dict]:
        """Active listings: [{listing_id, title, price, currency, url}]."""
        results = self._raw_search(keywords, limit).get("results", [])
        out = []
        for r in results:
            if not isinstance(r, dict):
                continue
            price, currency = _money(r.get("price"))
            out.append({
                "listing_id": r.get("listing_id"),
                "title": str(r.get("title", "")),
                "price": price,
                "currency": currency,
                "url": f"https://www.etsy.com/listing/{r.get('listing_id')}",
            })
        return out

    def stats(self, keywords: str) -> dict:
        """Competitor landscape: result count + price band + sample titles."""
        res = self._raw_search(keywords, 20)
        results = res.get("results", [])
        prices, currency = [], ""
        titles = []
        for r in results:
            if not isinstance(r, dict):
                continue
            price, cur = _money(r.get("price"))
            if price > 0:
                prices.append(price)
                currency = currency or cur
            title = str(r.get("title", "")).strip()
            if title and len(titles) < 5:
                titles.append(title)
        return {
            "keywords": keywords,
            "result_count": res.get("count", len(results)),
            "avg_price": round(sum(prices) / len(prices), 2) if prices else 0.0,
            "min_price": round(min(prices), 2) if prices else 0.0,
            "max_price": round(max(prices), 2) if prices else 0.0,
            "currency": currency,
            "sample_titles": titles,
        }


# -- Google Trends -----------------------------------------------------

class TrendsClient:
    """12-month search-interest direction via Google Trends (keyless).

    NOTE (2026-10-04): pytrends 4.9.2 is the latest release and is
    unmaintained. Its build_payload() POSTs the explore payload in the
    query string with an empty body, which Google now rejects with
    HTTP 411 (Length Required). We fetch the widget tokens with GET
    instead (accepted by the endpoint) and only use pytrends for the
    GET-based interest_over_time() call, which still works.
    """

    EXPLORE_URL = "https://trends.google.com/trends/api/explore"

    def __init__(self, geo: str = "US") -> None:
        self.geo = geo

    def _build_payload_via_get(self, pt, kws: list[str]) -> None:
        """Populate a TrendReq's TIMESERIES widget with a GET explore call."""
        import json

        import requests

        token_payload = {
            "hl": "en-US",
            "tz": 360,
            "req": json.dumps(
                {
                    "comparisonItem": [
                        {"keyword": kw, "geo": self.geo, "time": "today 12-m"}
                        for kw in kws
                    ],
                    "category": 0,
                    "property": "",
                }
            ),
        }
        session = requests.session()
        session.headers.update(pt.headers)
        resp = session.get(
            self.EXPLORE_URL, params=token_payload, timeout=(10, 25))
        if resp.status_code == 429:
            raise MarketDataUnavailable(
                "Google Trends rate-limited this IP (HTTP 429) — "
                "try again later or from another network")
        if resp.status_code != 200:
            raise MarketDataUnavailable(
                f"Google Trends explore failed: HTTP {resp.status_code}")
        try:
            data = json.loads(resp.text[4:])  # strip ")]}',"
        except Exception as exc:
            raise MarketDataUnavailable(
                f"could not parse Trends explore response: {exc}") from exc
        pt.kw_list = list(kws)
        pt.interest_over_time_widget = None
        for widget in data.get("widgets", []):
            if widget.get("id") == "TIMESERIES":
                pt.interest_over_time_widget = widget
                break
        if pt.interest_over_time_widget is None:
            raise MarketDataUnavailable(
                "Trends explore returned no TIMESERIES widget")

    def interest(self, keywords: list[str]) -> dict:
        """{keyword: {avg_12mo, trend (rising|flat|falling), latest}}."""
        kws = [k for k in keywords if k][:3]
        if not kws:
            return {}
        try:
            from pytrends.request import TrendReq
            pt = TrendReq(hl="en-US", tz=360, timeout=(10, 25))
            self._build_payload_via_get(pt, kws)
            df = pt.interest_over_time()
        except MarketDataUnavailable:
            raise
        except Exception as exc:
            raise MarketDataUnavailable(
                f"Google Trends request failed: {exc}") from exc
        out: dict[str, dict] = {}
        try:
            for kw in kws:
                if kw not in df.columns:
                    continue
                series = df[kw].dropna()
                if len(series) < 8:
                    continue
                half = len(series) // 2
                first = float(series.iloc[:half].mean())
                second = float(series.iloc[half:].mean())
                latest = float(series.iloc[-1])
                if second > first * 1.15:
                    trend = "rising"
                elif second < first * 0.85:
                    trend = "falling"
                else:
                    trend = "flat"
                out[kw] = {
                    "avg_12mo": round(float(series.mean()), 1),
                    "trend": trend,
                    "latest": round(latest, 1),
                }
        except Exception as exc:
            raise MarketDataUnavailable(
                f"could not parse Trends response: {exc}") from exc
        if not out:
            raise MarketDataUnavailable("Trends returned no usable data")
        return out


# -- Snapshot + TTL cache -----------------------------------------------

@dataclass
class MarketSnapshot:
    keywords: list[str]
    etsy_stats: dict = field(default_factory=dict)  # keyword -> stats dict
    trends: dict = field(default_factory=dict)      # keyword -> trend dict
    fetched_at: float = 0.0
    partial: bool = False


_cache: dict[tuple, tuple[MarketSnapshot, float]] = {}


def get_snapshot(keywords: list[str],
                 etsy_client: EtsyMarketClient | None = None,
                 trends_client: TrendsClient | None = None,
                 ttl_seconds: int = 3600) -> MarketSnapshot:
    """TTL-cached market snapshot (keyed by keyword set).

    Partial failure policy: if one source fails, return the other's data
    with partial=True. Raises MarketDataUnavailable only if BOTH fail
    (or no clients were given).
    """
    key = tuple(keywords)
    now = time.time()
    if key in _cache:
        snap, ts = _cache[key]
        if now - ts < ttl_seconds:
            return snap

    etsy_stats: dict = {}
    trends: dict = {}
    etsy_err: Exception | None = None
    trends_err: Exception | None = None

    if etsy_client is not None:
        try:
            for kw in keywords:
                etsy_stats[kw] = etsy_client.stats(kw)
        except MarketDataUnavailable as exc:
            etsy_err = exc
    if trends_client is not None:
        try:
            trends = trends_client.interest(list(keywords))
        except MarketDataUnavailable as exc:
            trends_err = exc

    if not etsy_stats and not trends:
        raise MarketDataUnavailable(
            f"all market sources failed (etsy: {etsy_err}; trends: {trends_err})")

    snap = MarketSnapshot(
        keywords=list(keywords), etsy_stats=etsy_stats, trends=trends,
        fetched_at=now, partial=bool(etsy_err or trends_err))
    _cache[key] = (snap, now)
    return snap


def clear_cache() -> None:
    """Drop all cached snapshots (tests / manual refresh)."""
    _cache.clear()


# -- Provider (what gets injected into the LLM sources) -----------------

class MarketDataProvider:
    """Owns the clients + formats the LIVE MARKET DATA prompt block."""

    DEFAULT_WATCHLIST = ["printable planner", "notion template",
                         "digital wall art"]

    def __init__(self,
                 etsy_client: EtsyMarketClient | None = None,
                 trends_client: TrendsClient | None = None,
                 ttl_seconds: int = 3600,
                 watchlist: list[str] | None = None) -> None:
        self.etsy_client = etsy_client
        self.trends_client = trends_client
        self.ttl_seconds = ttl_seconds
        self.watchlist = watchlist or list(self.DEFAULT_WATCHLIST)

    def available(self) -> bool:
        return self.etsy_client is not None or self.trends_client is not None

    def market_block(self, keywords: list[str]) -> str:
        """Formatted LIVE MARKET DATA block, or '' if unavailable.

        Never raises — returns '' on MarketDataUnavailable so prompts
        read fine with or without the block.
        """
        try:
            snap = get_snapshot(keywords,
                                etsy_client=self.etsy_client,
                                trends_client=self.trends_client,
                                ttl_seconds=self.ttl_seconds)
        except MarketDataUnavailable as exc:
            log.warning("market data unavailable, prompt without block: %s", exc)
            return ""
        lines = ["LIVE MARKET DATA (real numbers, fetched today):"]
        for kw in snap.keywords:
            parts = []
            st = snap.etsy_stats.get(kw)
            if st:
                parts.append(
                    f"Etsy: ~{st['result_count']} active listings, "
                    f"price band ${st['min_price']:.2f}-${st['max_price']:.2f} "
                    f"(avg ${st['avg_price']:.2f})")
            tr = snap.trends.get(kw)
            if tr:
                parts.append(
                    f"Google Trends 12mo: {tr['trend']} "
                    f"(avg {tr['avg_12mo']}, latest {tr['latest']})")
            if parts:
                lines.append(f"- {kw}: " + "; ".join(parts))
        if len(lines) == 1:
            return ""
        if snap.partial:
            lines.append("(note: one market source was briefly unavailable)")
        return "\n".join(lines)


# -- Composition helper --------------------------------------------------

def _load_etsy_creds() -> dict | None:
    """Etsy creds from env or ~/.config/evergreen-etsy (never raises)."""
    env = {
        "keystring": os.environ.get("ETSY_KEYSTRING"),
        "shared_secret": os.environ.get("ETSY_SHARED_SECRET"),
        "access_token": os.environ.get("ETSY_ACCESS_TOKEN"),
        "refresh_token": os.environ.get("ETSY_REFRESH_TOKEN"),
        "shop_id": os.environ.get("ETSY_SHOP_ID", "65154169"),
    }
    if all(env[k] for k in ("keystring", "shared_secret", "access_token",
                            "refresh_token")):
        return env
    try:
        import json

        cfg_path = Path.home() / ".config" / "evergreen-etsy" / "etsy_config.json"
        tok_path = Path.home() / ".config" / "evergreen-etsy" / "tokens.json"
        if not (cfg_path.exists() and tok_path.exists()):
            return None
        cfg = json.loads(cfg_path.read_text())
        tok = json.loads(tok_path.read_text())
        if not (cfg.get("keystring") and tok.get("access_token")):
            return None
        return {
            "keystring": cfg["keystring"],
            "shared_secret": cfg["shared_secret"],
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            "shop_id": os.environ.get("ETSY_SHOP_ID", "65154169"),
        }
    except Exception as exc:
        log.warning("could not load Etsy creds for market data: %s", exc)
        return None


def build_market_provider(ttl_seconds: int = 3600,
                          watchlist: list[str] | None = None) -> MarketDataProvider | None:
    """Construct the provider, or None when no source is usable."""
    etsy_client = None
    creds = _load_etsy_creds()
    if creds:
        try:
            from core.etsy_adapter import EtsyCommerceAdapter

            adapter = EtsyCommerceAdapter(
                creds["keystring"], creds["shared_secret"],
                creds["access_token"], creds["shop_id"])
            etsy_client = EtsyMarketClient(adapter, creds=creds)
        except Exception as exc:
            log.warning("Etsy market client disabled: %s", exc)
    trends_client = None
    try:
        trends_client = TrendsClient()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Trends client disabled: %s", exc)
    if etsy_client is None and trends_client is None:
        return None
    return MarketDataProvider(etsy_client=etsy_client,
                              trends_client=trends_client,
                              ttl_seconds=ttl_seconds, watchlist=watchlist)

"""Traffic metrics for a marketed website (Plausible Stats API, optional).

Env config (all optional — without a key, metrics come only from what
the owner records by hand via the growth program CLI):
    PLAUSIBLE_API_KEY    Plausible → Account settings → API keys
    PLAUSIBLE_SITE_ID    default: road2cissp.com
    PLAUSIBLE_BASE_URL   default: https://plausible.io (self-hosted works too)
"""

from __future__ import annotations

import json
import os
import urllib.request
from urllib.parse import urlencode


class MetricsUnavailable(RuntimeError):
    pass


class PlausibleClient:
    def __init__(self, api_key: str | None = None, site_id: str | None = None,
                 base_url: str | None = None, timeout: int = 20) -> None:
        self.api_key = api_key or os.environ.get("PLAUSIBLE_API_KEY", "")
        self.site_id = site_id or os.environ.get("PLAUSIBLE_SITE_ID", "road2cissp.com")
        self.base_url = (base_url or os.environ.get(
            "PLAUSIBLE_BASE_URL", "https://plausible.io")).rstrip("/")
        self.timeout = timeout

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("PLAUSIBLE_API_KEY"))

    def _get(self, path: str, **params) -> dict:
        if not self.api_key:
            raise MetricsUnavailable("PLAUSIBLE_API_KEY is not set")
        url = f"{self.base_url}{path}?{urlencode({'site_id': self.site_id, **params})}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise MetricsUnavailable(f"Plausible request failed: {exc}") from exc

    def snapshot(self, period: str = "7d") -> dict:
        """Aggregate traffic + top sources and pages for the period."""
        agg = self._get("/api/v1/stats/aggregate", period=period,
                        metrics="visitors,pageviews,bounce_rate,visit_duration")
        results = agg.get("results", {})
        out = {k: (v or {}).get("value") for k, v in results.items()}
        for prop, key in (("visit:source", "top_sources"), ("event:page", "top_pages")):
            try:
                rows = self._get("/api/v1/stats/breakdown", period=period,
                                 property=prop, limit=10).get("results", [])
            except MetricsUnavailable:
                rows = []
            label = prop.split(":")[1]
            out[key] = [{"name": r.get(label), "visitors": r.get("visitors")}
                        for r in rows]
        out["period"] = period
        out["source"] = "plausible"
        return out

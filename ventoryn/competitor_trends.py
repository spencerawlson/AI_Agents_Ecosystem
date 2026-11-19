"""Competitor tracking + trend analysis for Axionory (Holo gap 3).

Track what a site's rivals post and promote, and fold the findings
(and trend movements) into the weekly plan. Read-only: nothing here
publishes, posts, spends, signs up, or buys anything. Every finding
the plan shows names a human move; the owner acts on it.

Sources, in priority order:

1. DataForSEO — the planned source for competitor/keyword
   intelligence (see ~/workspace/marketing-api-registry/
   axionory-integration-plan.md). It needs Spencer's $50 top-up
   decision: no account is known. ``DataForSEOAdapter`` therefore
   reports ``not_configured`` until ``DATAFORSEO_LOGIN`` and
   ``DATAFORSEO_PASSWORD`` are both present in the environment.
   Credentials come from env only, are never stored, never logged,
   and never appear in code.
2. What already exists on main, no new account needed:
   ``LocalTrendsAdapter`` wraps an injected trends client with an
   ``.interest(keywords)`` method (the ``TrendsClient`` shape from
   ``core.market_data`` — keyless Google Trends, 12-month
   rising/flat/falling per keyword).

Provider contract (same shape as ``ventoryn.social_discovery``: an
injected client, no live call at import time, tests use fakes only).
``fetch`` never raises for a missing source — it returns a report::

    {"source": "<provider name>",
     "status": "ok" | "not_configured" | "unavailable",
     "reason": "<plain words>",
     "findings": [...], "trends": [...]}

Finding shape (normalized across providers)::

    {"competitor": "<name>", "kind": "post" | "promotion" | "keyword" | "trend",
     "title": "...", "text": "...", "url": "...",
     "observed_at": "<iso>", "source": "<provider name>"}

NEVER invent competitor data: when no source returns data, the report
is ``not_configured`` or ``unavailable`` with zero findings, and the
stored state / plan say "no competitor data yet". ``refresh_competitors``
stores findings only when the report status is ``"ok"``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from ventoryn.saas import SaasError
from ventoryn.social_oauth import _check, _client_for

NO_DATA_MESSAGE = "no competitor data yet"

DATAFORSEO_BASE = "https://api.dataforseo.com/v3"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower() or url
    except Exception:
        return url


def _finding(competitor: str, kind: str, title: str, text: str = "",
             url: str = "", source: str = "") -> dict:
    return {
        "competitor": competitor,
        "kind": kind,
        "title": title,
        "text": text,
        "url": url,
        "observed_at": _now_iso(),
        "source": source,
    }


def _empty_report(source: str, status: str, reason: str) -> dict:
    return {"source": source, "status": status, "reason": reason,
            "findings": [], "trends": []}


class CompetitorTrendProvider:
    """Base class for a competitor/trend source. Subclasses implement
    ``configured`` and ``fetch``; the client is always injected."""

    name = "base"

    def __init__(self, client=None) -> None:
        self.client = client

    @property
    def configured(self) -> bool:
        return True

    def fetch(self, competitors: list[dict],
              keywords: list[str]) -> dict:
        raise NotImplementedError


class DataForSEOAdapter(CompetitorTrendProvider):
    """DataForSEO competitor/keyword intelligence.

    Auth is HTTP Basic with the account login + password, from env
    only: ``DATAFORSEO_LOGIN`` / ``DATAFORSEO_PASSWORD``. No keys in
    code, no keys stored, no keys logged.

    With no credentials present the adapter honestly reports
    ``not_configured`` and makes no call at all — the $50 top-up /
    account decision is Spencer's step, and until he makes it this
    source stays off. A transport or API failure reports
    ``unavailable``; it never yields a fabricated finding.

    Endpoint mapping (provisional — the registry records DataForSEO's
    auth method as UNVERIFIED and no account exists yet, so re-verify
    against the live docs when the account is created):
    competitors come from ``dataforseo_labs/google/competitors_domain/
    live`` (shared-keyword overlap per rival domain); keyword volumes
    from ``dataforseo_labs/google/keyword_overview/live``. Parsing is
    defensive: a response missing the expected shape yields zero
    findings for that call, never invented numbers.
    """

    name = "dataforseo"

    def __init__(self, client=None, env: dict | None = None) -> None:
        super().__init__(client)
        env = os.environ if env is None else env
        self.login = (env.get("DATAFORSEO_LOGIN") or "").strip()
        self.password = (env.get("DATAFORSEO_PASSWORD") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.login and self.password)

    def _post(self, path: str, payload: list[dict]) -> dict:
        http = _client_for(self.client)
        return _check(http.post(
            f"{DATAFORSEO_BASE}{path}", json=payload,
            auth=(self.login, self.password)),
            "dataforseo", f"POST {path}")

    @staticmethod
    def _tasks(body: dict) -> list[dict]:
        tasks = body.get("tasks") if isinstance(body, dict) else None
        return tasks if isinstance(tasks, list) else []

    def fetch(self, competitors: list[dict],
              keywords: list[str]) -> dict:
        if not self.configured:
            return _empty_report(
                self.name, "not_configured",
                "DataForSEO is not configured — no account or top-up yet. "
                "Spencer's step: create the DataForSEO account and the "
                "$50 top-up, then set DATAFORSEO_LOGIN and "
                "DATAFORSEO_PASSWORD. Until then this source stays off "
                "and only existing sources feed trends.")
        findings: list[dict] = []
        try:
            for comp in competitors or []:
                name = str(comp.get("name") or comp.get("url") or "")
                domain = _domain_of(str(comp.get("url") or name))
                if not domain:
                    continue
                body = self._post(
                    "/dataforseo_labs/google/competitors_domain/live",
                    [{"target": domain, "limit": 10}])
                for task in self._tasks(body):
                    result = task.get("result") or []
                    for item in result if isinstance(result, list) else []:
                        if not isinstance(item, dict):
                            continue
                        rival = str(item.get("domain") or "")
                        shared = item.get("intersections")
                        if not rival:
                            continue
                        shared_txt = (f"shares {shared} keywords with "
                                      f"{domain}" if isinstance(shared, int)
                                      else f"competes with {domain}")
                        findings.append(_finding(
                            name, "keyword",
                            f"{rival} {shared_txt}",
                            f"DataForSEO competitors_domain for {domain}: "
                            f"rival {rival}"
                            + (f", avg. position "
                               f"{item.get('avg_position')}"
                               if item.get("avg_position") is not None
                               else ""),
                            f"https://{rival}", self.name))
            kws = [k for k in (keywords or []) if k][:10]
            if kws:
                body = self._post(
                    "/dataforseo_labs/google/keyword_overview/live",
                    [{"keywords": kws}])
                for task in self._tasks(body):
                    result = task.get("result") or []
                    for item in result if isinstance(result, list) else []:
                        if not isinstance(item, dict):
                            continue
                        kw = str(item.get("keyword") or "")
                        if not kw:
                            continue
                        vol = item.get("search_volume")
                        findings.append(_finding(
                            "", "keyword",
                            f"Keyword {kw!r}"
                            + (f": {vol} searches/mo"
                               if isinstance(vol, int) else ""),
                            "DataForSEO keyword_overview",
                            "", self.name))
        except SaasError as exc:
            return _empty_report(self.name, "unavailable", str(exc))
        except Exception as exc:  # transport failure, bad payload, ...
            return _empty_report(
                self.name, "unavailable",
                f"DataForSEO request failed: {exc}")
        return {"source": self.name, "status": "ok",
                "reason": f"{len(findings)} finding(s) from DataForSEO.",
                "findings": findings, "trends": []}


class LocalTrendsAdapter(CompetitorTrendProvider):
    """Trend analysis from what already exists on main: an injected
    trends client with ``.interest(keywords)`` (the ``TrendsClient``
    shape from ``core.market_data`` — keyless Google Trends, 12-month
    direction per keyword). Rising/falling keywords become ``trend``
    findings; flat ones are reported but not findings (no move to make).

    With no client injected the adapter reports ``unavailable`` — it
    never invents a trend.
    """

    name = "local_trends"

    def __init__(self, trends_client=None) -> None:
        super().__init__(client=None)
        self.trends_client = trends_client

    @property
    def configured(self) -> bool:
        return self.trends_client is not None

    def fetch(self, competitors: list[dict],
              keywords: list[str]) -> dict:
        if self.trends_client is None:
            return _empty_report(
                self.name, "unavailable",
                "No trends client was injected — Google Trends is "
                "unavailable in this run.")
        kws = [k for k in (keywords or []) if k]
        if not kws:
            return _empty_report(
                self.name, "unavailable",
                "No keywords to track — add keywords before trends "
                "can be measured.")
        try:
            interest = self.trends_client.interest(kws)
        except Exception as exc:
            return _empty_report(
                self.name, "unavailable",
                f"Google Trends request failed: {exc}")
        trends: list[dict] = []
        findings: list[dict] = []
        for kw in kws:
            data = (interest or {}).get(kw)
            if not isinstance(data, dict):
                continue
            direction = str(data.get("trend") or "")
            entry = {"keyword": kw, "trend": direction,
                     "avg_12mo": data.get("avg_12mo"),
                     "latest": data.get("latest")}
            trends.append(entry)
            if direction in ("rising", "falling"):
                findings.append(_finding(
                    "", "trend",
                    f"Trend: {kw!r} is {direction} "
                    f"(12-month avg {data.get('avg_12mo')}, "
                    f"latest {data.get('latest')})",
                    "Google Trends, 12-month interest direction.",
                    "", self.name))
        return {"source": self.name, "status": "ok",
                "reason": f"{len(trends)} keyword trend(s) measured.",
                "findings": findings, "trends": trends}


# -- storage bridge ------------------------------------------------------

def refresh_competitors(store, site_id: str,
                        provider: CompetitorTrendProvider,
                        keywords: list[str] | None = None) -> dict:
    """Fetch from one provider and store what came back — only when
    the report status is ``"ok"``. A ``not_configured`` or
    ``unavailable`` report stores nothing and leaves previous findings
    untouched (stale data is labelled stale by ``observed_at``, never
    refreshed by invention). Returns the provider's report unchanged,
    so callers can render its honest reason."""
    competitors = store.list_competitors(site_id)
    report = provider.fetch(competitors, list(keywords or []))
    if report.get("status") == "ok" and report.get("findings"):
        by_name = {str(c.get("name") or ""): c.get("id", "")
                   for c in competitors}
        for finding in report["findings"]:
            rival_id = by_name.get(str(finding.get("competitor") or ""))
            if rival_id:
                finding["competitor_id"] = rival_id
        store.record_competitor_findings(site_id, report["findings"])
    return report


def findings_for_plan(store, site_id: str, limit: int = 20) -> list[dict]:
    """Stored findings shaped for the weekly plan
    (``ecosystem.daily_plan.build_daily_plan`` reads them from
    ``state["competitor_findings"]``). Returns [] when there is nothing
    — the plan then renders the honest empty state, never an invented
    finding."""
    out = []
    for row in store.list_competitor_findings(site_id, limit=limit):
        out.append({
            "id": row.get("id", ""),
            "competitor": row.get("competitor", ""),
            "kind": row.get("kind", ""),
            "title": row.get("title", ""),
            "text": row.get("text", ""),
            "url": row.get("url", ""),
            "source": row.get("source", ""),
            "observed_at": row.get("observed_at", ""),
        })
    return out


def state_for_plan(store, site_id: str, limit: int = 20) -> dict:
    """The two state keys the weekly plan reads for gap 3: the tracked
    rivals (``state["competitors"]``, for the plan's ``tracked`` count)
    and their stored findings (``state["competitor_findings"]``). The
    caller merges this into the growth state before building the plan.
    Both are [] when there is nothing — the plan then says
    "no competitor data yet"."""
    return {
        "competitors": store.list_competitors(site_id),
        "competitor_findings": findings_for_plan(store, site_id,
                                                 limit=limit),
    }

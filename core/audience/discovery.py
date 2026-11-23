"""Run the planned searches across the available sources.

``run`` asks each source the planned queries, within its own limits,
and returns the evidence plus a coverage report: which sources were
searched (queries, results, errors, cost), which could not be and why,
and whether the scan stopped early. It never invents a result to fill a
gap: a failed or unavailable source is reported as such.

Protections:
- per-source pause between calls (``min_interval_s``) and query cap;
- a scan-wide call cap and time limit, and a stop check between calls
  (cancellation): whatever was found before stopping is kept;
- a source that fails twice in a row is stopped for this scan;
- duplicates are merged — the same URL, or the same text posted in
  several places — so copy-paste spam cannot inflate demand;
- results on the business's own site are dropped;
- text that addresses an AI is kept for a human but flagged.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.audience.providers import StackExchange
from core.audience.untrusted import clean, looks_like_injection

MAX_CALLS = 40
MAX_SECONDS = 120
_KEEP_PARAMS = {"v", "id", "p", "t", "q", "question", "topic", "thread"}


def normalize_url(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query)
                             if k.lower() in _KEEP_PARAMS))
    return urlunsplit(("https", host, p.path.rstrip("/") or "/", query, ""))


def _content_key(item: dict) -> str:
    text = " ".join(re.findall(r"[^\W_]+", (item["title"] + " " + item["snippet"]).lower()))
    return hashlib.sha256(text.encode()).hexdigest()[:20] if len(text) > 40 else ""


def _own(url: str, own_host: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    own = (own_host or "").lower()
    own = own[4:] if own.startswith("www.") else own
    host = host[4:] if host.startswith("www.") else host
    return bool(own) and (host == own or host.endswith("." + own))


def run(queries: list[dict], *, providers: list, http, env: dict, own_host: str = "",
        topic_terms: list[str] | None = None, core_terms: list[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        should_stop: Callable[[], bool] = lambda: False,
        on_cost: Callable[[str, float], None] | None = None,
        max_calls: int = MAX_CALLS, max_seconds: float = MAX_SECONDS) -> dict:
    started = clock()
    coverage: dict[str, dict] = {}
    raw: list[dict] = []
    calls = 0
    stop_reason = ""
    for p in providers:
        info = p.info
        cov = {"label": info.label, "kind": info.kind, "status": "searched", "reason": "",
               "queries": 0, "results": 0, "errors": [], "cost_usd": 0.0,
               "retention_days": info.retention_days}
        coverage[info.name] = cov
        ok, reason = p.availability(env)
        if not ok:
            cov.update(status="unavailable", reason=reason)
            continue
        if stop_reason:
            cov.update(status="skipped", reason=stop_reason)
            continue
        per_call = p.cost_per_call_usd(env)
        context = {}
        if isinstance(p, StackExchange):
            try:
                sites = p.pick_sites(topic_terms or [], p.sites(http, env),
                                     core=core_terms)
            except Exception as exc:  # noqa: BLE001
                cov.update(status="failed", reason=clean(exc, 160))
                continue
            if not sites:
                cov.update(status="skipped",
                           reason="no Stack Exchange site matches this business's topic")
                continue
            context["stackexchange_sites"] = sites
            cov["reason"] = "searched " + ", ".join(s["name"] for s in sites)
        failures, last = 0, None
        for q in queries[:info.max_queries]:
            if should_stop():
                stop_reason = "stopped on request"
            elif calls >= max_calls:
                stop_reason = f"reached the {max_calls}-call limit for one scan"
            elif clock() - started > max_seconds:
                stop_reason = f"reached the {int(max_seconds)}-second limit for one scan"
            if stop_reason:
                break
            if last is not None:
                wait = info.min_interval_s - (clock() - last)
                if wait > 0:
                    sleep(wait)
            last = clock()
            calls += 1
            cov["queries"] += 1
            try:
                results = p.search({**q, **context}, http, env)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                cov["errors"].append(f"{q['id']}: {clean(exc, 160)}")
                failures += 1
                if failures >= 2:
                    cov.update(status="failed", reason="stopped after two errors in a row")
                    break
                continue
            finally:
                if per_call is None:
                    cov["cost_usd"] = None
                elif cov["cost_usd"] is not None:
                    cov["cost_usd"] += per_call
                if on_cost is not None and per_call:
                    on_cost(info.name, per_call)
            failures = 0
            for r in results:
                r["query_ids"] = [q["id"]]
                raw.append(r)
            cov["results"] += len(results)
        if stop_reason and cov["status"] == "searched" and cov["queries"] < min(
                len(queries), info.max_queries):
            cov["reason"] = (cov["reason"] + "; " if cov["reason"] else "") + stop_reason
    items, dropped = _dedupe(raw, own_host)
    known = [c["cost_usd"] for c in coverage.values() if c["status"] != "unavailable"]
    return {"items": items, "coverage": coverage, "queries": queries,
            "stopped": stop_reason, "calls": calls, "dropped": dropped,
            "cost_usd": None if any(c is None for c in known) else round(sum(known), 4)}


def _dedupe(raw: list[dict], own_host: str) -> tuple[list[dict], dict]:
    by_url: dict[str, dict] = {}
    by_text: dict[str, dict] = {}
    dropped = {"own_site": 0, "duplicate_url": 0, "duplicate_text": 0}
    out: list[dict] = []
    for r in raw:
        if _own(r["url"], own_host):
            dropped["own_site"] += 1
            continue
        key = normalize_url(r["url"])
        text_key = _content_key(r)
        twin = by_url.get(key) or (by_text.get(text_key) if text_key else None)
        if twin is not None:
            dropped["duplicate_url" if key in by_url else "duplicate_text"] += 1
            for qid in r["query_ids"]:
                if qid not in twin["query_ids"]:
                    twin["query_ids"].append(qid)
            if r["source"] not in twin["sources"]:
                twin["sources"].append(r["source"])
            twin["copies"] += 1
            continue
        item = {**r, "url_key": key, "sources": [r["source"]], "copies": 1, "flags": []}
        if looks_like_injection(r["title"] + " " + r["snippet"]):
            item["flags"].append("contains text addressed to an AI")
        by_url[key] = item
        if text_key:
            by_text[text_key] = item
        out.append(item)
    return out, dropped

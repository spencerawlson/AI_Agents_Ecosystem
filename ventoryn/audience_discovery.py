"""Audience intelligence, phase 2: the demand-discovery job and its card.

``run_discovery_job`` takes the site's latest business profile, plans
the searches, runs them on every source this server can use, and
stores the evidence (see ``ventoryn/audience.py`` for storage,
retention and account scoping). Search calls with a known cost are
added to the job's usage, so they are priced and settled exactly like
AI calls. The result lists what was found *and* what could not be
searched, with the reason.

Phase 2 only finds and stores evidence. Classifying buyer intent and
ranking opportunities is phase 3: nothing here claims a result is a
buying signal.
"""

from __future__ import annotations

import gzip
import html
import json
import urllib.error
import urllib.request

from ventoryn import audience

USER_AGENT = "Axionory/1.0 (+https://www.axionory.com)"
MAX_JSON_BYTES = 3_000_000


def http_json(url: str, headers: dict) -> tuple[int, object]:
    """GET a provider API through the SSRF guard. Handles gzip (Stack
    Exchange always compresses). (status, parsed JSON or None)."""
    from core.netguard import safe_urlopen

    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json",
        "Accept-Encoding": "gzip", **headers})
    try:
        with safe_urlopen(req, timeout=15) as resp:
            raw = resp.read(MAX_JSON_BYTES)
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return resp.status, json.loads(raw or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:  # noqa: BLE001 - reported by the caller as a failed call
        return 0, None


def _record_cost(provider: str, cost_usd: float) -> None:
    from ventoryn.usage_metering import record_llm_usage

    record_llm_usage({"model": f"search/{provider}", "input_tokens": 0,
                      "output_tokens": 0, "cost_usd": cost_usd})


def run_discovery_job(store, site: dict, *, job_id: str = "", http=None, env=None,
                      providers=None, sleep=None) -> tuple[dict, str]:
    """Plan, search, store. Returns (result, scan_id). Raises
    RuntimeError (job refunded) when there is no profile to work from or
    no source could be searched at all."""
    import time

    from core.audience import discovery, providers as prov, query_plan

    aud = audience.audience_store_for(store)
    account_id = site["account_id"]
    profile = aud.latest(account_id, site["id"], audience.KIND_PROFILE)
    if profile is None or profile["status"] != "done":
        raise RuntimeError("build the business profile first — discovery searches "
                           "for what the profile says the site sells")
    claims = profile["claims"]
    queries = query_plan.plan(claims, language=site.get("language") or "en",
                              country=site.get("country") or "")
    if not queries:
        raise RuntimeError("the business profile has nothing to search for yet "
                           "(no offerings, terms or AI search phrases)")
    providers = providers if providers is not None else prov.registry()
    env = env if env is not None else prov.server_env()
    scan_id = aud.start_scan(account_id, site["id"], audience.KIND_DISCOVERY, job_id)
    try:
        result = discovery.run(
            queries, providers=providers, http=http or http_json, env=env,
            own_host=site.get("host") or "", topic_terms=query_plan.topic_terms(claims),
            core_terms=query_plan.core_terms(claims),
            sleep=sleep or time.sleep,
            should_stop=lambda: aud.stop_requested(account_id, scan_id),
            on_cost=_record_cost)
    except Exception as exc:  # noqa: BLE001
        aud.finish_scan(account_id, scan_id, status="failed", claims=[], error=str(exc)[:300])
        raise
    searched = [c for c in result["coverage"].values() if c["status"] == "searched"]
    detail = {"coverage": result["coverage"], "queries": result["queries"],
              "stopped": result["stopped"], "calls": result["calls"],
              "dropped": result["dropped"], "cost_usd": result["cost_usd"],
              "profile_scan": profile["id"]}
    if not searched and not result["stopped"]:
        aud.finish_scan(account_id, scan_id, status="failed", claims=[], detail=detail,
                        error="no source could be searched")
        raise RuntimeError("no source could be searched — "
                           + "; ".join(f"{c['label']}: {c['reason']}"
                                       for c in result["coverage"].values()
                                       if c["status"] != "unavailable"
                                       or c["kind"] != "social")[:300])
    retention = {p.info.name: p.info.retention_days for p in providers}
    aud.add_evidence(account_id, scan_id, result["items"], retention)
    summary = (f"{len(result['items'])} results from {len(searched)} sources, "
               f"{len(queries)} searches"
               + (f" · {result['stopped']}" if result["stopped"] else ""))
    aud.finish_scan(account_id, scan_id, status="done", claims=[], summary=summary,
                    detail=detail)
    return result, scan_id


# -- report and card -----------------------------------------------------

_STATUS = {"searched": "Searched", "unavailable": "Not available",
           "failed": "Failed", "skipped": "Skipped"}


def _cost_text(cost) -> str:
    if cost is None:
        return "cost not known on this server"
    return f"${cost:.3f}" if cost else "no charge"


def report_lines(site: dict, result: dict, date: str) -> list[str]:
    lines = [f"# Demand discovery — {site['url']} ({date})", "",
             "> Results are public pages that matched searches built from your "
             "business profile. They are not yet sorted by buying intent "
             "(that comes next). Nothing was posted or sent.", "",
             "## Searches", ""]
    for q in result["queries"]:
        lines.append(f"- {q['id']} “{q['text']}” — {q['why']} (from {', '.join(q['origin'])})")
    lines += ["", "## Sources", "", "| Source | Status | Searches | Results | Cost | Note |",
              "|---|---|---|---|---|---|"]
    for c in result["coverage"].values():
        lines.append(f"| {c['label']} | {_STATUS.get(c['status'], c['status'])} | "
                     f"{c['queries']} | {c['results']} | {_cost_text(c['cost_usd'])} | "
                     f"{c['reason'] or '—'} |")
    d = result["dropped"]
    lines += ["", f"Removed: {d['duplicate_url']} duplicate links, {d['duplicate_text']} "
              f"copies of the same text, {d['own_site']} pages on your own site.", "",
              "## Results", ""]
    for it in result["items"]:
        flag = " ⚠ contains text addressed to an AI" if it["flags"] else ""
        lines.append(f"- [{it['title'] or it['url']}]({it['url']}) — {it['venue']}"
                     f" · {(it['published_at'] or '')[:10]} · via {', '.join(it['sources'])}"
                     f" ({', '.join(it['query_ids'])}){flag}")
    if not result["items"]:
        lines.append("- Nothing matched.")
    return lines


def _e(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def render_card(site: dict, profile: dict | None, scan: dict | None,
                items: list[dict], running: dict | None, cost_credits: int) -> str:
    sid = _e(site["id"])
    head = ("<div class='card' id='demand-discovery'><h2>Where your customers are "
            "talking</h2>")
    if running is not None:
        stop = ("" if running["status"] == "stopping" else
                f"<form method='post' action='/websites/sites/{sid}/audience/stop' "
                f"style='margin:0'><input type='hidden' name='scan_id' "
                f"value='{_e(running['id'])}'><button class='btn' type='submit'>"
                f"Stop the search</button></form>")
        return (f"{head}<p class='muted'>A search is running"
                f"{' — stopping after the current call' if running['status'] == 'stopping' else ''}"
                f". Results found so far are kept.</p>{stop}</div>")
    if profile is None or profile.get("status") != "done":
        return (f"{head}<p class='muted'>Build the business profile above first: "
                f"Axionory searches for what your site actually sells.</p></div>")
    run = (f"<form method='post' action='/websites/sites/{sid}/jobs' style='margin:0'>"
           f"<input type='hidden' name='job_type' value='audience_discovery'>"
           f"<button class='btn' type='submit'>{'Search again' if scan else 'Search for demand'}"
           f" ({cost_credits} credits)</button></form>")
    if scan is None:
        return (f"{head}<p class='muted'>Axionory turns your profile into searches and "
                f"runs them on the sources it is allowed to use (Brave Search, Stack "
                f"Exchange, Hacker News, YouTube when configured), then lists the public "
                f"discussions and pages that match — and which sources it could not "
                f"search, and why.</p>{run}</div>")
    if scan["status"] != "done":
        return (f"{head}<p class='muted'>The last search failed "
                f"({_e(scan.get('error') or 'unknown error')}); its credits were "
                f"refunded.</p>{run}</div>")
    detail = scan.get("detail") or {}
    cov_rows = "".join(
        f"<tr><td>{_e(c['label'])}</td><td>{_e(_STATUS.get(c['status'], c['status']))}"
        f"</td><td>{c['results'] if c['status'] == 'searched' else '—'}</td>"
        f"<td class='muted'>{_e(c['reason'])}</td></tr>"
        for c in (detail.get("coverage") or {}).values())
    queries = "".join(f"<li>“{_e(q['text'])}” <span class='muted'>— {_e(q['why'])}"
                      f"</span></li>" for q in detail.get("queries") or [])
    shown = items[:30]
    results = "".join(
        f"<li><a href='{_e(it['url'])}' rel='nofollow noopener noreferrer' "
        f"target='_blank'>{_e(it['title'] or it['url'])}</a> "
        f"<span class='muted' style='font-size:12px'>{_e(it['venue'])}"
        f"{' · ' + _e(it['published_at'][:10]) if it['published_at'] else ''}"
        f" · {_e(', '.join(it['sources']))}</span>"
        + (" <span class='badge badge-off'>flagged</span>" if it["flags"] else "")
        + (f"<br><span class='muted' style='font-size:12px'>{_e(it['snippet'][:200])}</span>"
           if it["snippet"] else "") + "</li>"
        for it in shown)
    when = _e((scan.get("finished_at") or "")[:16].replace("T", " "))
    more = (f"<p class='muted'>Showing {len(shown)} of {len(items)}; the full list is "
            f"in the job report.</p>" if len(items) > len(shown) else "")
    return (f"{head}<p class='muted'>{_e(scan.get('summary'))} · {when} UTC · search "
            f"cost: {_e(_cost_text(detail.get('cost_usd')))}</p>"
            f"<details><summary><strong>Sources</strong></summary><div class='table-wrap'>"
            f"<table><tr><th>Source</th><th>Status</th><th>Results</th><th>Note</th></tr>"
            f"{cov_rows}</table></div></details>"
            f"<details><summary><strong>Searches</strong></summary><ul>{queries}</ul>"
            f"</details>"
            f"<details open><summary><strong>Results</strong></summary>"
            f"<ul>{results or '<li>Nothing matched.</li>'}</ul>{more}</details>"
            f"<p class='muted' style='margin-top:10px'>Public pages only, no names or "
            f"profiles of people. Not yet sorted by buying intent — that comes next. "
            f"Results expire after 30 days.</p>{run}</div>")

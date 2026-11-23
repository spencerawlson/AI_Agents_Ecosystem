"""Audience intelligence for Axionory customers: storage and the job.

Phase 2 — demand discovery. The ``audience_discovery`` job turns the
latest profile into searches (``core.audience.query_plan``), runs them
on the sources this server can use (``core.audience.discovery``), and
stores the evidence with an expiry date set by each source's
retention rule. Search costs are recorded with the job's usage, like
AI calls. A scan can be stopped from the site page; what was found
before stopping is kept.

Phase 1 — the business profile. The ``audience_profile`` job (one of
the site jobs, see ``ventoryn/site_jobs.py``) reads the customer's
website through the SSRF-guarded fetch, extracts claims with evidence
(``core.audience.website``), adds AI hypotheses grounded in those facts
when the server has an AI key (``core.audience.hypotheses``, metered by
the job like every other LLM call), and stores the result here.

Tenant isolation: every read and write takes the ``account_id`` and
filters on it in SQL, together with the site id — a scan id or site id
from another account returns nothing. There is no unscoped getter.

Retention: the latest ``KEEP_SCANS`` profile scans per site are kept;
older scans and their claims are deleted when a new one finishes.
Profiles hold the business's own public website text only — no data
about individual people.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS audience_scans (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audience_scans_site
    ON audience_scans (account_id, site_id, kind, created_at);
CREATE TABLE IF NOT EXISTS audience_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    basis TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS audience_claims_scan
    ON audience_claims (account_id, scan_id);
CREATE TABLE IF NOT EXISTS audience_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    source TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    kind TEXT NOT NULL DEFAULT '',
    venue TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    url_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    engagement TEXT NOT NULL DEFAULT '{}',
    query_ids TEXT NOT NULL DEFAULT '[]',
    copies INTEGER NOT NULL DEFAULT 1,
    flags TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audience_evidence_scan
    ON audience_evidence (account_id, scan_id);
"""

KEEP_SCANS = 5
KIND_PROFILE = "profile"
KIND_DISCOVERY = "discovery"

#: Shown on the profile page, in this order.
FIELD_LABELS = {
    "name": "Business name",
    "description": "How the site describes itself",
    "value_proposition": "Headline promises",
    "offering": "Products and services",
    "price": "Prices",
    "audience": "Who the site says it is for",
    "language": "Languages",
    "area_served": "Areas served",
    "location": "Location",
    "keyword": "Recurring terms",
    "market": "Likely market",
    "currency": "Currency",
    "segment": "Customer segments",
    "problem": "Problems customers want solved",
    "motivation": "Reasons to buy now",
    "barrier": "What could stop them",
    "alternative": "Alternatives they may compare",
    "search_phrase": "What buyers might search for",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AudienceStore:
    """Shares the SaaS store's SQLite connection and lock."""

    def __init__(self, saas_store):
        self._conn, self._lock = saas_store._conn, saas_store._lock
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def start_scan(self, account_id: str, site_id: str, kind: str,
                   job_id: str = "") -> str:
        scan_id = secrets.token_hex(12)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audience_scans (id, account_id, site_id, kind, status,"
                " created_at, job_id) VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (scan_id, account_id, site_id, kind, _now(), job_id))
        return scan_id

    def finish_scan(self, account_id: str, scan_id: str, *, status: str,
                    claims: list[dict], summary: str = "", error: str = "",
                    detail: dict | None = None) -> None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT site_id, kind FROM audience_scans WHERE id = ? AND account_id = ?",
                (scan_id, account_id)).fetchone()
            if row is None:
                return
            site_id, kind = row[0], row[1]
            self._conn.executemany(
                "INSERT INTO audience_claims (scan_id, account_id, site_id, claim_id,"
                " field, value, kind, confidence, evidence, basis)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(scan_id, account_id, site_id, c.get("id", ""), c["field"], c["value"],
                  c["kind"], c.get("confidence", ""),
                  json.dumps(c.get("evidence", [])), json.dumps(c.get("basis", [])))
                 for c in claims])
            self._conn.execute(
                "UPDATE audience_scans SET status = ?, finished_at = ?, summary = ?,"
                " error = ?, detail = ? WHERE id = ? AND account_id = ?",
                (status, _now(), summary[:500], error[:500],
                 json.dumps(detail or {}), scan_id, account_id))
            old = [r[0] for r in self._conn.execute(
                "SELECT id FROM audience_scans WHERE account_id = ? AND site_id = ?"
                " AND kind = ? AND status NOT IN ('running', 'stopping')"
                " ORDER BY created_at DESC"
                " LIMIT -1 OFFSET ?", (account_id, site_id, kind, KEEP_SCANS))]
            for sid in old:
                self._conn.execute("DELETE FROM audience_claims WHERE scan_id = ?"
                                   " AND account_id = ?", (sid, account_id))
                self._conn.execute("DELETE FROM audience_evidence WHERE scan_id = ?"
                                   " AND account_id = ?", (sid, account_id))
                self._conn.execute("DELETE FROM audience_scans WHERE id = ?"
                                   " AND account_id = ?", (sid, account_id))

    def latest(self, account_id: str, site_id: str,
               kind: str = KIND_PROFILE) -> dict | None:
        """The newest finished scan of this kind with its claims."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM audience_scans WHERE account_id = ? AND site_id = ?"
                " AND kind = ? AND status NOT IN ('running', 'stopping')"
                " ORDER BY created_at DESC LIMIT 1",
                (account_id, site_id, kind)).fetchone()
            if row is None:
                return None
            scan = dict(zip([d[0] for d in self._conn.execute(
                "SELECT * FROM audience_scans LIMIT 0").description], row))
            rows = self._conn.execute(
                "SELECT claim_id, field, value, kind, confidence, evidence, basis"
                " FROM audience_claims WHERE scan_id = ? AND account_id = ?"
                " ORDER BY id", (scan["id"], account_id)).fetchall()
        scan["detail"] = json.loads(scan.get("detail") or "{}")
        scan["claims"] = [{"id": r[0], "field": r[1], "value": r[2], "kind": r[3],
                           "confidence": r[4], "evidence": json.loads(r[5]),
                           "basis": json.loads(r[6])} for r in rows]
        return scan

    # -- discovery evidence ------------------------------------------------
    def add_evidence(self, account_id: str, scan_id: str, items: list[dict],
                     retention_days: dict[str, int]) -> int:
        """Store a scan's evidence. Each row expires after its source's
        retention period; expired rows are deleted on every write."""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT site_id FROM audience_scans WHERE id = ? AND account_id = ?",
                (scan_id, account_id)).fetchone()
            if row is None:
                return 0
            self._conn.execute("DELETE FROM audience_evidence WHERE expires_at < ?",
                               (now.isoformat(),))
            rows = []
            for it in items:
                days = max(1, int(retention_days.get(it["source"], 30)))
                rows.append((scan_id, account_id, row[0], it["source"],
                             json.dumps(it.get("sources") or [it["source"]]),
                             it.get("kind", ""), it.get("venue", ""), it["url"],
                             it.get("url_key", ""), it.get("title", ""),
                             it.get("snippet", ""), it.get("published_at", ""),
                             json.dumps(it.get("engagement") or {}),
                             json.dumps(it.get("query_ids") or []),
                             int(it.get("copies") or 1), json.dumps(it.get("flags") or []),
                             now.isoformat(), (now + timedelta(days=days)).isoformat()))
            self._conn.executemany(
                "INSERT INTO audience_evidence (scan_id, account_id, site_id, source,"
                " sources, kind, venue, url, url_key, title, snippet, published_at,"
                " engagement, query_ids, copies, flags, fetched_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def evidence(self, account_id: str, scan_id: str) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM audience_evidence WHERE account_id = ? AND scan_id = ?"
                " AND expires_at >= ? ORDER BY id", (account_id, scan_id, now))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            for k in ("sources", "engagement", "query_ids", "flags"):
                r[k] = json.loads(r[k] or "null")
        return rows

    # -- stopping a running scan ------------------------------------------
    def running(self, account_id: str, site_id: str, kind: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, status, created_at FROM audience_scans WHERE account_id = ?"
                " AND site_id = ? AND kind = ? AND status IN ('running', 'stopping')"
                " ORDER BY created_at DESC LIMIT 1",
                (account_id, site_id, kind)).fetchone()
        return {"id": row[0], "status": row[1], "created_at": row[2]} if row else None

    def request_stop(self, account_id: str, scan_id: str) -> bool:
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE audience_scans SET status = 'stopping' WHERE id = ?"
                " AND account_id = ? AND status = 'running'",
                (scan_id, account_id)).rowcount == 1

    def stop_requested(self, account_id: str, scan_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM audience_scans WHERE id = ? AND account_id = ?",
                (scan_id, account_id)).fetchone()
        return bool(row) and row[0] == "stopping"

    def delete_site(self, account_id: str, site_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM audience_claims WHERE account_id = ?"
                               " AND site_id = ?", (account_id, site_id))
            self._conn.execute("DELETE FROM audience_evidence WHERE account_id = ?"
                               " AND site_id = ?", (account_id, site_id))
            self._conn.execute("DELETE FROM audience_scans WHERE account_id = ?"
                               " AND site_id = ?", (account_id, site_id))


def audience_store_for(saas_store) -> AudienceStore:
    s = getattr(saas_store, "_ventoryn_audience", None)
    if s is None:
        s = AudienceStore(saas_store)
        saas_store._ventoryn_audience = s
    return s


def _default_fetch(url: str) -> dict:
    from ventoryn.site_snapshot import fetch_page

    return fetch_page(url)


def _default_gateway():
    from core.llm import LLMGateway

    return LLMGateway() if LLMGateway.enabled() else None


def build_profile(site: dict, *, fetch=None, gateway="default") -> dict:
    """Read the site and build the profile (no storage). Returns
    {"url", "pages", "skipped", "claims", "error", "ai_note"}."""
    from core.audience import hypotheses, website

    fetch = fetch or _default_fetch
    result = website.collect(site["url"], fetch=fetch, user_agent="VentorynSnapshot")
    result.pop("texts", None)
    if gateway == "default":
        gateway = _default_gateway()
    ai_note = ""
    if result.get("error"):
        ai_note = "skipped: the website could not be read"
    elif gateway is None:
        ai_note = "no AI key on the server — facts only"
    else:
        hyps, ai_note = hypotheses.generate(result["claims"], gateway,
                                            language=site.get("language") or "en")
        start = len(result["claims"])
        for i, h in enumerate(hyps, start + 1):
            h["id"] = f"h{i - start}"
        result["claims"] += hyps
    result["ai_note"] = ai_note
    return result


def run_profile_job(store, site: dict, *, job_id: str = "", fetch=None,
                    gateway="default") -> tuple[dict, str]:
    """Build and store a profile for ``site`` (scoped to its account).
    Returns (profile, scan_id). Raises RuntimeError when the home page
    cannot be read, so the job is refunded."""
    aud = audience_store_for(store)
    scan_id = aud.start_scan(site["account_id"], site["id"], KIND_PROFILE, job_id)
    try:
        profile = build_profile(site, fetch=fetch, gateway=gateway)
    except Exception as exc:  # noqa: BLE001
        aud.finish_scan(site["account_id"], scan_id, status="failed", claims=[],
                        error=str(exc)[:300])
        raise
    counts = _counts(profile["claims"])
    summary = (f"{counts['fact']} facts, {counts['derived']} derived, "
               f"{counts['hypothesis']} AI hypotheses from {len(profile['pages'])} pages")
    if profile.get("error"):
        aud.finish_scan(site["account_id"], scan_id, status="failed", claims=[],
                        error=profile["error"],
                        detail={"pages": profile["pages"]})
        raise RuntimeError(f"the website could not be read ({profile['error']})")
    aud.finish_scan(site["account_id"], scan_id, status="done", claims=profile["claims"],
                    summary=summary, detail={"pages": profile["pages"],
                                             "skipped": profile["skipped"],
                                             "ai_note": profile["ai_note"]})
    return profile, scan_id


def _counts(claims: list[dict]) -> dict:
    out = {"fact": 0, "derived": 0, "hypothesis": 0}
    for c in claims:
        out[c["kind"]] = out.get(c["kind"], 0) + 1
    return out


def report_lines(site: dict, profile: dict, date: str) -> list[str]:
    """The Markdown report the job writes (same content as the page)."""
    lines = [f"# Business profile — {site['url']} ({date})", "",
             "> Facts are quoted from the website. Hypotheses are AI guesses "
             "built on those facts — check them before acting on them.", "",
             f"Pages read: {len(profile['pages'])}"
             + (f" · skipped: {len(profile['skipped'])}" if profile["skipped"] else ""),
             ""]
    by_field: dict[str, list[dict]] = {}
    for c in profile["claims"]:
        by_field.setdefault(c["field"], []).append(c)
    for field, label in FIELD_LABELS.items():
        items = by_field.get(field)
        if not items:
            continue
        lines += [f"## {label}", ""]
        for c in items:
            ev = c["evidence"][0] if c["evidence"] else {}
            src = f" — [{ev.get('where', 'source')}]({ev.get('url', '')})" if ev else ""
            tag = {"fact": "", "derived": " *(derived)*",
                   "hypothesis": " *(AI hypothesis)*"}.get(c["kind"], "")
            lines.append(f"- {c['value']}{tag}{src}")
        lines.append("")
    if profile.get("ai_note"):
        lines += [f"AI hypotheses: {profile['ai_note']}", ""]
    return lines


# -- site page card ------------------------------------------------------

_KIND_BADGE = {"fact": "", "derived": " <span class='badge'>derived</span>",
               "hypothesis": " <span class='badge badge-off'>AI guess</span>"}
_GROUPS = (
    ("From your website", ("name", "description", "value_proposition", "offering",
                           "price", "audience", "language", "area_served",
                           "location")),
    ("Patterns and signals", ("keyword", "market", "currency")),
    ("AI hypotheses — check before acting", ("segment", "problem", "motivation",
                                             "barrier", "alternative",
                                             "search_phrase")),
)


def _esc(value) -> str:
    import html

    return html.escape(str(value or ""), quote=True)


def _claim_html(c: dict) -> str:
    ev = c["evidence"][0] if c.get("evidence") else None
    source = ""
    if ev:
        quote = _esc(ev.get("quote"))
        url = ev.get("url") or ""
        link = (f"<a href='{_esc(url)}' rel='nofollow noopener' target='_blank'>"
                f"{_esc(ev.get('where') or 'source')}</a>"
                if url.startswith(("http://", "https://")) else _esc(ev.get("where")))
        source = (f"<br><span class='muted' style='font-size:12px'>{link}"
                  + (f" — “{quote}”" if quote and c["kind"] != "hypothesis" else "")
                  + "</span>")
    basis = (f" <span class='muted' style='font-size:12px'>(rests on "
             f"{_esc(', '.join(c.get('basis') or []))})</span>"
             if c["kind"] == "hypothesis" and c.get("basis") else "")
    cid = f"<span class='muted' style='font-size:12px'>{_esc(c.get('id'))}</span> "
    return f"<li>{cid}{_esc(c['value'])}{_KIND_BADGE.get(c['kind'], '')}{basis}{source}</li>"


def render_card(site: dict, scan: dict | None, cost_credits: int) -> str:
    """The Business profile card for the site page."""
    sid = _esc(site["id"])
    run = (f"<form method='post' action='/websites/sites/{sid}/jobs' style='margin:0'>"
           f"<input type='hidden' name='job_type' value='audience_profile'>"
           f"<button class='btn' type='submit'>"
           f"{'Refresh' if scan else 'Build'} business profile ({cost_credits} credits)"
           f"</button></form>")
    head = "<div class='card' id='business-profile'><h2>Business profile</h2>"
    if scan is None:
        return (f"{head}<p class='muted'>Axionory reads up to 8 pages of "
                f"{_esc(site['host'])} — home, pricing, products, about, FAQ — and "
                f"lists what it sells, to whom and where, each fact linked to the "
                f"page it came from. This is the starting point for finding where "
                f"your customers are.</p>{run}</div>")
    if scan["status"] != "done":
        return (f"{head}<p class='muted'>The last profile could not be built "
                f"({_esc(scan.get('error') or 'unknown error')}). Nothing was "
                f"charged for it.</p>{run}</div>")
    by_field: dict[str, list[dict]] = {}
    for c in scan["claims"]:
        by_field.setdefault(c["field"], []).append(c)
    sections = []
    for title, fields in _GROUPS:
        parts = []
        for f in fields:
            items = by_field.get(f)
            if items:
                parts.append(f"<h4 style='margin:10px 0 4px'>{_esc(FIELD_LABELS[f])}</h4>"
                             f"<ul>{''.join(_claim_html(c) for c in items)}</ul>")
        if parts:
            sections.append(f"<details{' open' if not sections else ''}>"
                            f"<summary><strong>{_esc(title)}</strong></summary>"
                            f"{''.join(parts)}</details>")
    detail = scan.get("detail") or {}
    note = detail.get("ai_note") or ""
    skipped = detail.get("skipped") or []
    when = _esc((scan.get("finished_at") or "")[:16].replace("T", " "))
    meta = (f"<p class='muted'>{_esc(scan.get('summary'))} · {when} UTC"
            + (f" · {len(skipped)} page(s) skipped (robots.txt or off-site)"
               if skipped else "")
            + (f" · AI: {_esc(note)}" if note else "") + "</p>")
    return (f"{head}{meta}{''.join(sections) or '<p>Nothing could be read.</p>'}"
            f"<p class='muted' style='margin-top:10px'>Facts quote your own pages. "
            f"AI guesses are built only on those facts and name the facts they "
            f"rest on — no numbers are invented.</p>{run}</div>")

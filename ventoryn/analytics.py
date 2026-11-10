"""Ventoryn Analytics: first-party, cookieless site analytics.

A customer adds one line to their site:

    <script defer data-site="<site id>" src="https://ventoryn.app/v.js"></script>

and Ventoryn records page views (plus custom events such as
``ventoryn('signup')``) for that site. This replaces the need for a
Plausible account for the funnel, the traffic-source breakdown and —
the part Plausible cannot do — evidence that a visitor arrived through
a *signed Ventoryn link* (the ``vtn_`` tag in ``utm_content``).

Privacy (the Plausible model):
- No cookies, no local storage, nothing stored on the visitor's device.
- A visitor is a one-way hash of (daily secret salt, site, IP, user
  agent). The salt changes every UTC day and old salts are deleted, so
  the same person cannot be linked across days and the hash cannot be
  reversed to an IP.
- Raw events are kept only for the current day. Completed days are
  rolled up into daily totals (per source, per event, per signed tag)
  and the raw rows — hashes included — are deleted.
- IPs and user agents are never stored.

Abuse protection on the public collect endpoint: the page URL (and the
Origin header, when sent) must be on the registered site's host; bots
and automation are dropped; bodies over 2 KB are refused; each client
IP is rate-limited.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

from fastapi import Request
from fastapi.responses import Response

MAX_BODY_BYTES = 2048
DIRECT = "Direct / None"
INTERNAL = "(internal)"
_EVENT_NAME = re.compile(r"^[a-z0-9_-]{1,40}$")
_BOT_UA = re.compile(
    r"bot|crawl|spider|slurp|preview|headless|lighthouse|pingdom|monitor|"
    r"curl|wget|python-requests|httpx|go-http-client|phantom|selenium",
    re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS va_salts (
    day TEXT PRIMARY KEY, salt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS va_events (
    site_id TEXT NOT NULL, day TEXT NOT NULL, ts TEXT NOT NULL,
    visitor TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,
    source TEXT NOT NULL, tag TEXT);
CREATE INDEX IF NOT EXISTS idx_va_events_site_day ON va_events(site_id, day);
CREATE TABLE IF NOT EXISTS va_daily (
    site_id TEXT NOT NULL, day TEXT NOT NULL, source TEXT NOT NULL,
    visitors INTEGER NOT NULL, pageviews INTEGER NOT NULL,
    PRIMARY KEY (site_id, day, source));
CREATE TABLE IF NOT EXISTS va_daily_events (
    site_id TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL,
    events INTEGER NOT NULL, visitors INTEGER NOT NULL,
    PRIMARY KEY (site_id, day, name));
CREATE TABLE IF NOT EXISTS va_daily_tags (
    site_id TEXT NOT NULL, day TEXT NOT NULL, tag TEXT NOT NULL,
    visitors INTEGER NOT NULL,
    PRIMARY KEY (site_id, day, tag));
CREATE TABLE IF NOT EXISTS va_sites (
    site_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL);
"""

# The tracking script. Kept small and dependency-free; sendBeacon with
# a text/plain body needs no CORS preflight.
TRACKER_JS = r"""(function(){
var d=document,s=d.currentScript;if(!s)return;
var site=s.getAttribute('data-site');if(!site||navigator.webdriver)return;
var api=s.getAttribute('data-api')||new URL(s.src).origin+'/api/va/collect';
function send(n){try{var b=JSON.stringify({s:site,n:n,u:location.href,r:d.referrer||''});
if(navigator.sendBeacon&&navigator.sendBeacon(api,new Blob([b],{type:'text/plain'})))return;
fetch(api,{method:'POST',body:b,keepalive:true,credentials:'omit',headers:{'Content-Type':'text/plain'}});}catch(e){}}
var last;function pv(){if(location.href===last)return;last=location.href;send('pageview');}
pv();var h=history;if(h.pushState){var p=h.pushState;h.pushState=function(){p.apply(this,arguments);pv();};addEventListener('popstate',pv);}
window.ventoryn=function(n){if(typeof n==='string'&&/^[a-z0-9_-]{1,40}$/i.test(n))send(n.toLowerCase());};
})();
"""


#: Ventoryn's own site in Ventoryn Analytics (ventoryn.app tracks itself).
#: VENTORYN_OWN_SITE_ID overrides it; "off" turns it off (e.g. dev/test).
OWN_SITE_ID_DEFAULT = "8e7c68c8885a1ef3e072a01c"


def own_site_id() -> str:
    """Ventoryn's own site id, or "" when turned off / invalid."""
    import os

    site = os.environ.get("VENTORYN_OWN_SITE_ID", OWN_SITE_ID_DEFAULT).strip()
    return site if re.fullmatch(r"[0-9a-f]{24}", site) else ""


def own_snippet() -> str:
    """The analytics tag for Ventoryn's own pages. Same origin as the app,
    so the platform CSP ('self') already allows the script and beacon."""
    site = own_site_id()
    if not site:
        return ""
    return f'<script defer data-site="{site}" src="https://ventoryn.app/v.js"></script>'


def _bare_host(host: str) -> str:
    host = (host or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def host_matches(candidate: str, site_host: str) -> bool:
    """example.com, www.example.com and shop.example.com all belong
    to a site registered as example.com (or www.example.com)."""
    c, s = _bare_host(candidate), _bare_host(site_host)
    return bool(c) and (c == s or c.endswith("." + s))


def is_bot(user_agent: str) -> bool:
    return not user_agent or bool(_BOT_UA.search(user_agent))


def snippet(site_id: str, base_url: str) -> str:
    return (f'<script defer data-site="{site_id}" '
            f'src="{base_url.rstrip("/")}/v.js"></script>')


class SiteAnalytics:
    """Storage + queries. Shares the SaaS store's SQLite connection."""

    def __init__(self, saas_store) -> None:
        self._conn = saas_store._conn
        self._lock = saas_store._lock
        self._saas = saas_store
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
        self._rolled_through: str | None = None

    # -- recording ------------------------------------------------------
    def _salt(self, day: str) -> str:
        row = self._conn.execute(
            "SELECT salt FROM va_salts WHERE day = ?", (day,)).fetchone()
        if row:
            return row[0]
        salt = secrets.token_hex(16)
        self._conn.execute("INSERT INTO va_salts (day, salt) VALUES (?, ?)",
                           (day, salt))
        # Older salts are deleted: yesterday's visitors can no longer be
        # re-identified, even by Ventoryn.
        self._conn.execute("DELETE FROM va_salts WHERE day < ?", (day,))
        return salt

    def record(self, site: dict, name: str, url: str, referrer: str,
               ip: str, user_agent: str, now: datetime | None = None) -> bool:
        """Record one event. Returns False when it is dropped."""
        from ventoryn.signature import parse_tag, tag_from_url

        now = now or datetime.now(timezone.utc)
        if not _EVENT_NAME.match(name or ""):
            return False
        parts = urlsplit(url or "")
        if parts.scheme not in ("http", "https") or not host_matches(
                parts.hostname or "", site["host"]):
            return False
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        ref_host = urlsplit(referrer or "").hostname or ""
        utm = (params.get("utm_source") or "").strip().lower()[:40]
        if utm:
            source = utm
        elif ref_host and host_matches(ref_host, site["host"]):
            source = INTERNAL
        elif ref_host:
            source = _bare_host(ref_host)[:80]
        else:
            source = DIRECT
        tag = tag_from_url(url)
        if tag and not (parse_tag(tag) and
                        self._saas.get_signature_by_tag(site["id"], tag)):
            tag = None  # only tags Ventoryn actually issued for this site
        day = now.strftime("%Y-%m-%d")
        with self._lock, self._conn:
            salt = self._salt(day)
            visitor = hashlib.sha256(
                f"{salt}|{site['id']}|{ip}|{user_agent}".encode()
            ).hexdigest()[:24]
            self._conn.execute(
                "INSERT INTO va_events (site_id, day, ts, visitor, name, "
                "path, source, tag) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (site["id"], day, now.isoformat(), visitor, name,
                 (parts.path or "/")[:300], source, tag))
            self._conn.execute(
                "INSERT INTO va_sites (site_id, first_seen, last_seen) "
                "VALUES (?, ?, ?) ON CONFLICT(site_id) DO UPDATE SET "
                "last_seen = excluded.last_seen",
                (site["id"], now.isoformat(), now.isoformat()))
        self.rollup(now)
        return True

    # -- aggregation ----------------------------------------------------
    def _aggregate_raw(self, site_id: str, day: str) -> dict:
        rows = self._conn.execute(
            "SELECT visitor, name, source, tag FROM va_events "
            "WHERE site_id = ? AND day = ? ORDER BY ts",
            (site_id, day)).fetchall()
        entry: dict[str, str] = {}
        views: dict[str, int] = {}
        events: dict[str, list] = {}
        tags: dict[str, set] = {}
        for visitor, name, source, tag in rows:
            if name == "pageview":
                views[visitor] = views.get(visitor, 0) + 1
                if source != INTERNAL and visitor not in entry:
                    entry[visitor] = source
            else:
                e = events.setdefault(name, [0, set()])
                e[0] += 1
                e[1].add(visitor)
            if tag:
                tags.setdefault(tag, set()).add(visitor)
        sources: dict[str, list] = {}
        for visitor, n in views.items():
            src = entry.get(visitor, DIRECT)
            agg = sources.setdefault(src, [0, 0])
            agg[0] += 1
            agg[1] += n
        return {"sources": {k: tuple(v) for k, v in sources.items()},
                "events": {k: (v[0], len(v[1])) for k, v in events.items()},
                "tags": {k: len(v) for k, v in tags.items()}}

    def rollup(self, now: datetime | None = None) -> None:
        """Fold every completed day into daily totals and delete its raw
        events (hashes included). Cheap to call often: it runs once per
        day per process."""
        now = now or datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if self._rolled_through == today:
            return
        with self._lock, self._conn:
            pending = self._conn.execute(
                "SELECT DISTINCT site_id, day FROM va_events WHERE day < ?",
                (today,)).fetchall()
            for site_id, day in pending:
                agg = self._aggregate_raw(site_id, day)
                for src, (visitors, views) in agg["sources"].items():
                    self._conn.execute(
                        "INSERT OR REPLACE INTO va_daily VALUES (?, ?, ?, ?, ?)",
                        (site_id, day, src, visitors, views))
                for name, (count, visitors) in agg["events"].items():
                    self._conn.execute(
                        "INSERT OR REPLACE INTO va_daily_events VALUES (?, ?, ?, ?, ?)",
                        (site_id, day, name, count, visitors))
                for tag, visitors in agg["tags"].items():
                    self._conn.execute(
                        "INSERT OR REPLACE INTO va_daily_tags VALUES (?, ?, ?, ?)",
                        (site_id, day, tag, visitors))
            self._conn.execute("DELETE FROM va_events WHERE day < ?", (today,))
            self._conn.execute("DELETE FROM va_salts WHERE day < ?", (today,))
        self._rolled_through = today

    # -- queries --------------------------------------------------------
    def summary(self, site_id: str, days: int = 30,
                now: datetime | None = None) -> dict:
        """Totals for the last ``days`` days (today included).

        Visitors are counted per day (the hash changes daily), exactly
        like Plausible: a person who visits on 3 days counts 3 times."""
        now = now or datetime.now(timezone.utc)
        self.rollup(now)
        today = now.strftime("%Y-%m-%d")
        start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        sources: dict[str, list] = {}
        events: dict[str, list] = {}
        tags: dict[str, int] = {}
        with self._lock:
            for src, v, p in self._conn.execute(
                    "SELECT source, SUM(visitors), SUM(pageviews) FROM va_daily "
                    "WHERE site_id = ? AND day >= ? GROUP BY source",
                    (site_id, start)):
                sources[src] = [v, p]
            for name, c, v in self._conn.execute(
                    "SELECT name, SUM(events), SUM(visitors) FROM va_daily_events "
                    "WHERE site_id = ? AND day >= ? GROUP BY name",
                    (site_id, start)):
                events[name] = [c, v]
            for tag, v in self._conn.execute(
                    "SELECT tag, SUM(visitors) FROM va_daily_tags "
                    "WHERE site_id = ? AND day >= ? GROUP BY tag",
                    (site_id, start)):
                tags[tag] = v
            live = self._aggregate_raw(site_id, today)
            seen = self._conn.execute(
                "SELECT first_seen, last_seen FROM va_sites WHERE site_id = ?",
                (site_id,)).fetchone()
        for src, (v, p) in live["sources"].items():
            agg = sources.setdefault(src, [0, 0])
            agg[0] += v
            agg[1] += p
        for name, (c, v) in live["events"].items():
            agg = events.setdefault(name, [0, 0])
            agg[0] += c
            agg[1] += v
        for tag, v in live["tags"].items():
            tags[tag] = tags.get(tag, 0) + v
        return {
            "installed": seen is not None,
            "first_seen": seen[0] if seen else None,
            "last_seen": seen[1] if seen else None,
            "visitors": sum(v for v, _ in sources.values()),
            "pageviews": sum(p for _, p in sources.values()),
            "sources": sorted(((s, v) for s, (v, _) in sources.items()),
                              key=lambda x: -x[1]),
            "events": {k: {"count": c, "visitors": v}
                       for k, (c, v) in events.items()},
            "signed_visits": sum(tags.values()),
            "tags": tags,
        }


def funnel_cells(saas_store, site: dict, plausible_visitors_fn=None) -> tuple[str, str]:
    """(visitors_html, signups_html) for a site's funnel row.

    Source of truth, in order: Ventoryn Analytics when its snippet has
    sent data; Plausible when connected; otherwise "unavailable". Never
    an invented number. ``plausible_visitors_fn(api_key, domain)`` is
    injected so callers keep their existing Plausible client."""
    import html as _html

    va = analytics_for(saas_store).summary(site["id"])
    if va["installed"]:
        visitors = (f"{va['visitors']:,} <span class='muted'>"
                    f"(Ventoryn Analytics)</span>")
        signup = va["events"].get("signup")
        if signup:
            signups = f"{signup['count']:,}"
        else:
            signups = ("<span class='unavailable'>none recorded — call "
                       "<code>ventoryn('signup')</code> when someone signs "
                       "up</span>")
        return visitors, signups
    signups = ("<span class='unavailable'>unavailable — signup tracking is "
               "not connected yet</span>")
    conn = saas_store.get_connection(site["id"], "plausible") or {}
    raw = conn.get("_detail_raw") or {}
    key = raw.get("api_key") if conn.get("status") == "connected" else None
    if key and plausible_visitors_fn is not None:
        v = plausible_visitors_fn(key, raw.get("site_domain") or site["host"])
        visitors = (f"{v:,}" if v is not None else
                    "<span class='unavailable'>unavailable — Plausible did "
                    "not answer</span>")
    else:
        visitors = (f"<span class='unavailable'>unavailable — install "
                    f"<a href='/account/sites/{_html.escape(site['id'])}"
                    f"#ventoryn-analytics'>Ventoryn Analytics</a></span>")
    return visitors, signups


def analytics_for(saas_store) -> SiteAnalytics:
    """One SiteAnalytics per SaaS store (cached on the store)."""
    va = getattr(saas_store, "_ventoryn_analytics", None)
    if va is None:
        va = SiteAnalytics(saas_store)
        saas_store._ventoryn_analytics = va
    return va


def check_install(site: dict, fetch=None) -> tuple[bool, str]:
    """Fetch the site's home page and look for the snippet."""
    if fetch is None:
        from core.site_audit import http_fetch as fetch
    status, body = fetch(site["url"])
    if status == 0:
        return False, "Ventoryn could not reach your site."
    if status >= 400:
        return False, f"Your site answered HTTP {status}."
    if "/v.js" in body and site["id"] in body:
        return True, ("The Ventoryn Analytics snippet is on your home page. "
                      "Visits appear here as they happen.")
    if "/v.js" in body:
        return False, ("A Ventoryn snippet is on the page, but with a "
                       "different site id — copy the snippet above again.")
    return False, ("The snippet is not on your home page yet. Add it "
                   "inside <head> on every page, then check again.")


def register_analytics_routes(app, saas_store, config) -> None:
    """GET /v.js and POST /api/va/collect (public)."""
    from ventoryn.analyze_jobs import preview_client_ip
    from ventoryn.security import RateLimiter

    va = analytics_for(saas_store)
    limiter = RateLimiter(limit=120, window=60)
    app.state.va_limiter = limiter

    @app.get("/v.js")
    def tracker_script():
        return Response(TRACKER_JS, media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=86400",
                                 "Access-Control-Allow-Origin": "*",
                                 "Cross-Origin-Resource-Policy": "cross-origin"})

    @app.post("/api/va/collect")
    async def collect(request: Request):
        # Always 204 with no detail: a probe learns nothing about which
        # site ids exist or why an event was dropped.
        done = Response(status_code=204,
                        headers={"Access-Control-Allow-Origin": "*"})
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return Response(status_code=413)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return Response(status_code=413)
        ua = request.headers.get("user-agent", "")
        if is_bot(ua):
            return done
        ip = preview_client_ip(request.headers,
                               request.client.host if request.client else "")
        if not limiter.allow(ip):
            return Response(status_code=429, headers={"Retry-After": "60"})
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return done
        if not isinstance(data, dict):
            return done
        site = saas_store.get_site(str(data.get("s", ""))[:64])
        if site is None:
            return done
        origin = request.headers.get("origin")
        if origin and not host_matches(urlsplit(origin).hostname or "",
                                       site["host"]):
            return done
        va.record(site, str(data.get("n", "pageview"))[:40],
                  str(data.get("u", ""))[:2000], str(data.get("r", ""))[:2000],
                  ip, ua[:400])
        return done

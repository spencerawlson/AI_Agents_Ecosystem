"""Website snapshot: what Ventoryn can see about a site the moment it is added.

Four sections, each built only from real, public sources — a value that
cannot be measured reads "unavailable" or "not connected", never a guess:

- Preview  — title, description, and (when the browser sandbox is
             available) a screenshot of the home page.
- Specs    — platform, hosting/CDN, HTTPS, speed, page weight, mobile
             readiness, SEO basics, structured data, sitemap, llms.txt,
             analytics already installed, social profiles linked.
- Traffic  — Ventoryn Analytics (real numbers, once installed); the
             Tranco top-sites rank (free); Google's Chrome UX Report
             (whether real Chrome visitors are measurable; needs
             GOOGLE_CRUX_API_KEY). Nobody can see a site's real visitor
             count from outside, so this is a size band, not a number.
- Presence — pages found in search and mentions elsewhere (Brave Search;
             needs BRAVE_SEARCH_API_KEY), Hacker News, the Internet
             Archive, and which AI crawlers robots.txt lets in.

Every fetch goes through core.netguard (no internal addresses, redirect
hops re-checked). The snapshot runs in a background thread when a site
is added and can be refreshed from the site page.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (compatible; VentorynSnapshot/1.0; +https://ventoryn.app)"
MAX_PAGE = 3_000_000
REFRESH_COOLDOWN_S = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS saas_site_snapshots (
    site_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    data TEXT
);
"""

_PLATFORMS = (
    ("WordPress", (r"wp-content/", r"<meta[^>]+generator[^>]+wordpress")),
    ("Shopify", (r"cdn\.shopify\.com", r"Shopify\.theme")),
    ("Wix", (r"static\.wixstatic\.com", r"<meta[^>]+generator[^>]+wix")),
    ("Squarespace", (r"static1\.squarespace\.com", r"squarespace-cdn")),
    ("Webflow", (r"webflow\.(com|io)", r"data-wf-page")),
    ("Ghost", (r"<meta[^>]+generator[^>]+ghost",)),
    ("Drupal", (r"<meta[^>]+generator[^>]+drupal", r"/sites/default/files/")),
    ("Joomla", (r"<meta[^>]+generator[^>]+joomla",)),
    ("Framer", (r"framerusercontent\.com",)),
    ("Next.js", (r"__NEXT_DATA__", r"/_next/static/")),
    ("Nuxt", (r"__NUXT__", r"/_nuxt/")),
    ("Gatsby", (r"___gatsby",)),
    ("Astro", (r"astro-island", r"<meta[^>]+generator[^>]+astro")),
)
_SOCIAL = (
    ("X / Twitter", r"(?:twitter|x)\.com/(?!intent|share|home)[A-Za-z0-9_]{2,}"),
    ("Instagram", r"instagram\.com/[A-Za-z0-9_.]{2,}"),
    ("Facebook", r"facebook\.com/(?!sharer|dialog)[A-Za-z0-9.\-]{2,}"),
    ("LinkedIn", r"linkedin\.com/(?:company|in)/[A-Za-z0-9\-_%]{2,}"),
    ("YouTube", r"youtube\.com/(?:@|c/|channel/|user/)[A-Za-z0-9\-_]{2,}"),
    ("TikTok", r"tiktok\.com/@[A-Za-z0-9_.]{2,}"),
    ("GitHub", r"github\.com/[A-Za-z0-9\-]{2,}"),
    ("Reddit", r"reddit\.com/(?:r|user)/[A-Za-z0-9_]{2,}"),
)
_ANALYTICS = (
    ("Ventoryn Analytics", "/v.js"), ("Google Analytics", "googletagmanager.com"),
    ("Google Analytics", "google-analytics.com"), ("Plausible", "plausible.io"),
    ("Cloudflare Web Analytics", "cloudflareinsights.com"),
    ("Vercel Analytics", "/_vercel/insights"), ("Microsoft Clarity", "clarity.ms"),
    ("PostHog", "posthog"), ("Umami", "umami"), ("Fathom", "usefathom"),
    ("Matomo", "matomo"), ("Meta Pixel", "connect.facebook.net"),
)


def _item(label, value, status="ok", note=""):
    return {"label": label, "value": value, "status": status, "note": note}


# -- network ----------------------------------------------------------------

def fetch_page(url: str, timeout: float = 15) -> dict:
    """GET through the SSRF guard: status, body, headers, timing, size."""
    from core.netguard import safe_urlopen

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    started = time.monotonic()
    try:
        with safe_urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_PAGE)
            return {"status": resp.status, "final_url": resp.geturl(),
                    "headers": {k.lower(): v for k, v in resp.headers.items()},
                    "body": raw.decode("utf-8", errors="replace"),
                    "bytes": len(raw), "ms": int((time.monotonic() - started) * 1000)}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "final_url": url, "headers": {}, "body": "",
                "bytes": 0, "ms": int((time.monotonic() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 - reported as unreachable
        return {"status": 0, "final_url": url, "headers": {}, "body": "",
                "bytes": 0, "ms": 0, "error": str(exc)[:200]}


def http_json(url: str, *, headers: dict | None = None,
              data: dict | None = None, timeout: float = 12):
    """(status, parsed JSON or None) for a third-party API, via the guard."""
    from core.netguard import safe_urlopen

    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with safe_urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read(2_000_000) or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:  # noqa: BLE001
        return 0, None


# -- page parsing -------------------------------------------------------------

class _Head(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.lang, self.meta = "", "", {}
        self.h1 = 0
        self.jsonld = 0
        self.links: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and key not in self.meta:
                self.meta[key] = a.get("content", "")
        elif tag == "h1":
            self.h1 += 1
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self.jsonld += 1
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


def detect_platform(body: str) -> str | None:
    for name, patterns in _PLATFORMS:
        if any(re.search(p, body, re.I) for p in patterns):
            return name
    return None


def detect_hosting(headers: dict) -> str | None:
    server = headers.get("server", "").lower()
    if "cf-ray" in headers or server == "cloudflare":
        return "Cloudflare"
    for key, name in (("x-vercel-id", "Vercel"), ("x-nf-request-id", "Netlify"),
                      ("x-amz-cf-id", "Amazon CloudFront"),
                      ("x-github-request-id", "GitHub Pages"),
                      ("x-shopid", "Shopify"), ("x-wix-request-id", "Wix"),
                      ("fly-request-id", "Fly.io"), ("x-render-origin-server", "Render")):
        if key in headers:
            return name
    if "x-served-by" in headers and "cache-" in headers.get("x-served-by", ""):
        return "Fastly"
    for token, name in (("nginx", "nginx"), ("apache", "Apache"),
                        ("litespeed", "LiteSpeed"), ("caddy", "Caddy"),
                        ("gws", "Google")):
        if token in server:
            return name
    return server or None


def social_profiles(body: str) -> list[str]:
    found = []
    for name, pattern in _SOCIAL:
        if re.search(pattern, body, re.I):
            found.append(name)
    return found


def analytics_installed(body: str) -> list[str]:
    seen = []
    for name, marker in _ANALYTICS:
        if marker.lower() in body.lower() and name not in seen:
            seen.append(name)
    return seen


def size_band(tranco_rank: int | None, crux_listed: bool | None) -> str:
    if tranco_rank:
        if tranco_rank <= 10_000:
            return "Very high — among the 10,000 most visited sites"
        if tranco_rank <= 100_000:
            return "High — among the 100,000 most visited sites"
        return "Established — in the top 1 million sites"
    if crux_listed:
        return "Measurable — enough real Chrome visitors for Google to report"
    if crux_listed is False:
        return "Small — below public ranking thresholds"
    return "Not ranked publicly"


# -- the snapshot ---------------------------------------------------------------

def collect(site: dict, *, env=None, page_fetch=fetch_page, json_get=http_json,
            screenshot=None, analytics=None) -> dict:
    env = os.environ if env is None else env
    page = page_fetch(site["url"])
    final = page.get("final_url") or site["url"]
    parts = urllib.parse.urlsplit(final)
    origin = f"{parts.scheme}://{parts.netloc}"
    domain = (parts.hostname or site["host"]).lower()
    bare = domain[4:] if domain.startswith("www.") else domain
    body, headers = page.get("body", ""), page.get("headers", {})
    reachable = page.get("status", 0) and page["status"] < 400
    head = _Head()
    try:
        head.feed(body)
    except Exception:  # noqa: BLE001 - malformed HTML still yields what parsed
        pass

    # -- preview
    preview = {"reachable": bool(reachable), "status": page.get("status", 0),
               "title": head.title.strip()[:200],
               "description": (head.meta.get("description")
                               or head.meta.get("og:description", ""))[:300],
               "site_name": head.meta.get("og:site_name", "")[:100],
               "final_url": final, "screenshot": None, "screenshot_note": ""}
    if screenshot is not None and reachable:
        path, note = screenshot(final)
        preview["screenshot"], preview["screenshot_note"] = path, note

    # -- specs
    robots_status, robots_body = 0, ""
    sitemap_ok = llms_ok = False
    if reachable:
        r = page_fetch(origin + "/robots.txt")
        robots_status, robots_body = r.get("status", 0), r.get("body", "")
        sitemap_ok = ("sitemap:" in robots_body.lower()
                      or page_fetch(origin + "/sitemap.xml").get("status") == 200)
        llms_ok = page_fetch(origin + "/llms.txt").get("status") == 200
    specs = []
    if not reachable:
        specs.append(_item("Reachable", f"No — {page.get('error') or 'HTTP ' + str(page.get('status'))}",
                           "unavailable"))
    else:
        specs += [
            _item("Platform", detect_platform(body) or "Custom / not detected"),
            _item("Hosting / CDN", detect_hosting(headers) or "Not detected"),
            _item("HTTPS", "Yes" + (" + HSTS" if "strict-transport-security" in headers else "")
                  if parts.scheme == "https" else "No", "ok" if parts.scheme == "https" else "warn"),
            _item("Response time", f"{page['ms']:,} ms", "ok" if page["ms"] < 1500 else "warn"),
            _item("Home page weight", f"{page['bytes'] / 1024:,.0f} KB"),
            _item("Mobile-ready", "Yes" if "viewport" in head.meta else "No viewport tag",
                  "ok" if "viewport" in head.meta else "warn"),
            _item("Language", head.lang or "Not declared", "ok" if head.lang else "warn"),
            _item("Title", f"{len(head.title.strip())} characters" if head.title.strip() else "Missing",
                  "ok" if 10 <= len(head.title.strip()) <= 65 else "warn"),
            _item("Meta description", f"{len(preview['description'])} characters"
                  if preview["description"] else "Missing",
                  "ok" if 50 <= len(preview["description"]) <= 160 else "warn"),
            _item("H1 headings", str(head.h1), "ok" if head.h1 == 1 else "warn"),
            _item("Structured data", f"{head.jsonld} JSON-LD block(s)" if head.jsonld else "None",
                  "ok" if head.jsonld else "warn"),
            _item("robots.txt", "Present" if robots_status == 200 else "Missing",
                  "ok" if robots_status == 200 else "warn"),
            _item("Sitemap", "Found" if sitemap_ok else "Not found", "ok" if sitemap_ok else "warn"),
            _item("llms.txt (for AI assistants)", "Present" if llms_ok else "Missing",
                  "ok" if llms_ok else "warn"),
            _item("Analytics installed", ", ".join(analytics_installed(body)) or "None detected"),
            _item("Social profiles linked", ", ".join(social_profiles(body)) or "None found"),
        ]

    # -- traffic
    traffic = []
    va = analytics(site["id"]) if analytics else None
    if va and va.get("installed"):
        traffic.append(_item("Visitors, last 30 days (Ventoryn Analytics)",
                             f"{va['visitors']:,} visitors · {va['pageviews']:,} page views"))
    else:
        traffic.append(_item("Visitors (real numbers)", "Install Ventoryn Analytics to see them",
                             "not_connected"))
    tranco_rank = None
    st, data = json_get(f"https://tranco-list.eu/api/ranks/domain/{urllib.parse.quote(bare)}")
    ranks = (data or {}).get("ranks") if isinstance(data, dict) else None
    if st == 200 and ranks:
        tranco_rank = int(sorted(ranks, key=lambda r: r.get("date", ""))[-1]["rank"])
        traffic.append(_item("Global popularity rank (Tranco)", f"#{tranco_rank:,}"))
    elif st == 200:
        traffic.append(_item("Global popularity rank (Tranco)", "Not in the top 1 million"))
    else:
        traffic.append(_item("Global popularity rank (Tranco)", "Unavailable", "unavailable"))
    crux_listed = None
    crux_key = env.get("GOOGLE_CRUX_API_KEY", "")
    if not crux_key:
        traffic.append(_item("Real Chrome visitors (Google CrUX)", "Not connected", "not_connected",
                             "Add GOOGLE_CRUX_API_KEY on the server"))
    else:
        st, data = json_get(
            "https://chromeuxreport.googleapis.com/v1/records:queryRecord?key="
            + urllib.parse.quote(crux_key), data={"origin": origin})
        if st == 200 and data:
            crux_listed = True
            lcp = (((data.get("record") or {}).get("metrics") or {})
                   .get("largest_contentful_paint") or {}).get("percentiles", {}).get("p75")
            traffic.append(_item("Real Chrome visitors (Google CrUX)",
                                 "Measured by Google" + (f" · page loads in {int(lcp) / 1000:.1f} s for most visitors"
                                                         if lcp else "")))
        elif st == 404:
            crux_listed = False
            traffic.append(_item("Real Chrome visitors (Google CrUX)",
                                 "Too few visitors for Google to report yet"))
        else:
            traffic.append(_item("Real Chrome visitors (Google CrUX)", "Unavailable", "unavailable"))
    traffic.insert(0, _item("Traffic size", size_band(tranco_rank, crux_listed),
                            "ok" if (tranco_rank or crux_listed is not None) else "unavailable"))

    # -- presence
    presence = []
    brave_key = env.get("BRAVE_SEARCH_API_KEY", "")
    if not brave_key:
        presence.append(_item("In search results (Brave Search)", "Not connected", "not_connected",
                              "Add BRAVE_SEARCH_API_KEY on the server"))
    else:
        hdr = {"X-Subscription-Token": brave_key}
        base = "https://api.search.brave.com/res/v1/web/search?count=20&q="
        st, data = json_get(base + urllib.parse.quote(f"site:{bare}"), headers=hdr)
        pages = len(((data or {}).get("web") or {}).get("results") or []) if st == 200 else None
        presence.append(_item("Pages found in search (Brave)",
                              ("20+ pages" if pages == 20 else f"{pages} page(s)")
                              if pages is not None else "Unavailable",
                              "ok" if pages else ("warn" if pages == 0 else "unavailable")))
        st, data = json_get(base + urllib.parse.quote(f'"{bare}" -site:{bare}'), headers=hdr)
        mentions = ((data or {}).get("web") or {}).get("results") or [] if st == 200 else None
        if mentions is None:
            presence.append(_item("Mentioned on other sites", "Unavailable", "unavailable"))
        else:
            hosts = []
            for m in mentions:
                h = urllib.parse.urlsplit(m.get("url", "")).hostname or ""
                if h and h not in hosts:
                    hosts.append(h)
            presence.append(_item("Mentioned on other sites",
                                  (", ".join(hosts[:6]) + (f" and {len(hosts) - 6} more" if len(hosts) > 6 else ""))
                                  if hosts else "No mentions found", "ok" if hosts else "warn"))
    st, data = json_get("https://hn.algolia.com/api/v1/search?restrictSearchableAttributes=url"
                        "&hitsPerPage=3&query=" + urllib.parse.quote(bare))
    if st == 200 and isinstance(data, dict):
        hits = int(data.get("nbHits") or 0)
        presence.append(_item("Hacker News", f"{hits:,} submission(s)" if hits else "Never submitted"))
    else:
        presence.append(_item("Hacker News", "Unavailable", "unavailable"))
    st, data = json_get("https://web.archive.org/cdx/search/cdx?output=json&limit=1&fl=timestamp&url="
                        + urllib.parse.quote(bare))
    if st == 200 and isinstance(data, list) and len(data) > 1:
        ts = str(data[1][0])
        presence.append(_item("Internet Archive", f"First saved {ts[:4]}-{ts[4:6]}"))
    elif st == 200:
        presence.append(_item("Internet Archive", "Never archived"))
    else:
        presence.append(_item("Internet Archive", "Unavailable", "unavailable"))
    if robots_status == 200:
        from core.geo_audit import AI_BOTS, parse_robots

        parsed = parse_robots(robots_body)
        allowed = [label for label, agent in AI_BOTS if parsed.get(agent) != "disallow"]
        presence.append(_item("AI assistants allowed to read it",
                              f"{len(allowed)} of {len(AI_BOTS)} crawlers",
                              "ok" if len(allowed) == len(AI_BOTS) else "warn"))
    elif reachable:
        presence.append(_item("AI assistants allowed to read it", "All (no robots.txt)"))

    return {"collected_at": datetime.now(timezone.utc).isoformat(),
            "preview": preview, "specs": specs, "traffic": traffic,
            "presence": presence}


# -- storage + background run ----------------------------------------------------

class SnapshotStore:
    def __init__(self, saas_store):
        self._conn, self._lock = saas_store._conn, saas_store._lock
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def get(self, site_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, started_at, finished_at, data FROM saas_site_snapshots "
                "WHERE site_id = ?", (site_id,)).fetchone()
        if row is None:
            return None
        return {"status": row[0], "started_at": row[1], "finished_at": row[2],
                "data": json.loads(row[3]) if row[3] else None}

    def mark_running(self, site_id: str) -> bool:
        """Claim a run; False if one is running or the last finished too
        recently (refresh cooldown)."""
        now = datetime.now(timezone.utc)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT status, started_at, finished_at FROM saas_site_snapshots "
                "WHERE site_id = ?", (site_id,)).fetchone()
            if row is not None:
                if row[0] == "running" and (
                        now - datetime.fromisoformat(row[1])).total_seconds() < 300:
                    return False
                if row[2] and (now - datetime.fromisoformat(row[2])
                               ).total_seconds() < REFRESH_COOLDOWN_S:
                    return False
            self._conn.execute(
                "INSERT INTO saas_site_snapshots (site_id, status, started_at) VALUES (?, 'running', ?) "
                "ON CONFLICT(site_id) DO UPDATE SET status = 'running', started_at = excluded.started_at",
                (site_id, now.isoformat()))
        return True

    def finish(self, site_id: str, status: str, data: dict | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_site_snapshots SET status = ?, finished_at = ?, "
                "data = COALESCE(?, data) WHERE site_id = ?",
                (status, datetime.now(timezone.utc).isoformat(),
                 json.dumps(data) if data is not None else None, site_id))


def snapshot_store_for(saas_store) -> SnapshotStore:
    s = getattr(saas_store, "_ventoryn_snapshots", None)
    if s is None:
        s = SnapshotStore(saas_store)
        saas_store._ventoryn_snapshots = s
    return s


def start(saas_store, site: dict, reports_dir, *, collector=None, force: bool = False) -> bool:
    """Run a snapshot in the background. False when one is already
    running or the refresh cooldown applies."""
    snaps = snapshot_store_for(saas_store)
    if not snaps.mark_running(site["id"]) and not force:
        return False

    def _run():
        try:
            from ventoryn.analytics import analytics_for
            from ventoryn.site_screenshot import capture_for_site

            run = collector or collect
            data = run(site, analytics=analytics_for(saas_store).summary,
                       screenshot=lambda url: capture_for_site(url, site["id"], reports_dir))
            snaps.finish(site["id"], "done", data)
        except Exception as exc:  # noqa: BLE001 - recorded, page shows it
            snaps.finish(site["id"], "failed", {"error": str(exc)[:300]})

    threading.Thread(target=_run, daemon=True, name=f"snapshot-{site['id']}").start()
    return True


# -- rendering ----------------------------------------------------------------------

_BADGE = {"ok": "", "warn": " v-snap-warn", "unavailable": " v-snap-na",
          "not_connected": " v-snap-na"}

SNAPSHOT_CSS = """
.v-snap-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.v-snap-grid > .card { margin: 0; min-width: 0; }
.v-snap-shot { display: block; width: 100%; height: auto; border-radius: 10px;
  border: 1px solid var(--v-line); margin: 6px 0 12px; }
.v-snap-list { list-style: none; padding: 0; margin: 0; }
.v-snap-list li { display: flex; justify-content: space-between; gap: 12px;
  padding: 8px 0; border-top: 1px solid var(--v-line); font-size: 14px; }
.v-snap-list li:first-child { border-top: 0; }
.v-snap-list .v { text-align: right; font-weight: 600; overflow-wrap: anywhere; }
.v-snap-list .v.v-snap-warn { color: var(--v-amber); }
.v-snap-list .v.v-snap-na { color: var(--v-muted); font-weight: 500; }
.v-snap-list .n { display: block; font-size: 12px; color: var(--v-muted); font-weight: 400; }
@media (max-width: 760px) { .v-snap-grid { grid-template-columns: minmax(0, 1fr); } }
"""


def _list(items) -> str:
    return "<ul class='v-snap-list'>" + "".join(
        f"<li><span>{html.escape(i['label'])}</span><span class='v{_BADGE.get(i['status'], '')}'>"
        f"{html.escape(i['value'])}"
        + (f"<span class='n'>{html.escape(i['note'])}</span>" if i.get("note") else "")
        + "</span></li>" for i in items) + "</ul>"


def render_card(site: dict, snap: dict | None) -> str:
    from ventoryn.icons import icon

    sid = html.escape(site["id"])
    back = f"<a class='v-back' href='/portfolio'>{icon('arrow_left', 15)} Websites</a>"
    refresh = (f"<form method='post' action='/websites/sites/{sid}/snapshot' style='margin:0'>"
               f"<button class='btn' type='submit'>Refresh snapshot</button></form>")
    if snap is None or (snap["status"] == "running" and not snap.get("data")):
        return (f"{back}<div class='card' id='site-snapshot'><h2>Website snapshot</h2>"
                "<p class='muted'>Looking at your site — preview, specs, traffic and where it "
                "appears online. This takes a few seconds.</p>"
                "<script>setTimeout(function(){location.reload()},6000)</script></div>")
    data = snap.get("data") or {}
    if snap["status"] == "failed" or "error" in data:
        return (f"{back}<div class='card' id='site-snapshot'><h2>Website snapshot</h2>"
                f"<p class='muted'>The snapshot could not finish "
                f"({html.escape(data.get('error', 'unknown error'))}).</p>{refresh}</div>")
    p = data["preview"]
    shot = ""
    if p.get("screenshot"):
        # The ONE bitmap Ventoryn shows: the owner's own site, on their own
        # site page (see tests/test_vector_only.py).
        shot = (f"<img class='v-snap-shot' src='/websites/sites/{sid}/snapshot-image' "
                f"alt='Screenshot of {html.escape(site['host'])}' width='1280' height='800'>")
    elif p.get("screenshot_note"):
        shot = f"<p class='muted'>Screenshot: {html.escape(p['screenshot_note'])}</p>"
    when = html.escape((data.get("collected_at") or "")[:16].replace("T", " "))
    running = " · refreshing…" if snap["status"] == "running" else ""
    preview = (f"<div class='card'><h2>Preview</h2>{shot}"
               f"<p><strong>{html.escape(p.get('title') or site['host'])}</strong></p>"
               f"<p class='muted'>{html.escape(p.get('description') or 'No description set.')}</p></div>")
    return (f"{back}<div id='site-snapshot'><div style='display:flex;justify-content:space-between;"
            f"align-items:center;gap:12px;flex-wrap:wrap'><h2 style='margin:0'>Website snapshot</h2>"
            f"<span class='muted'>Checked {when} UTC{running}</span>{refresh}</div>"
            f"<div class='v-snap-grid' style='margin-top:12px'>{preview}"
            f"<div class='card'><h2>Specs</h2>{_list(data['specs'])}</div>"
            f"<div class='card'><h2>Traffic</h2>{_list(data['traffic'])}"
            f"<p class='muted' style='margin-top:10px'>Nobody can see a site's real visitor count "
            f"from outside — this is a size band from public data. Ventoryn Analytics shows the "
            f"real numbers.</p></div>"
            f"<div class='card'><h2>Where it appears online</h2>{_list(data['presence'])}</div>"
            f"</div></div>")

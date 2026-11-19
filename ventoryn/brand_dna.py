"""Brand DNA: learn a brand from its website URL in one step.

Paste a website URL (the existing Add-website step) and Axionory learns
the brand: tone of voice, colours, products, audience, buying triggers.
The profile is shown on the site page and fed to content generation
(`campaigns.draft_posts`), so drafts sound like the brand instead of
generic copy.

Honesty rule, same as the snapshot: every field is built only from
evidence actually on the fetched home page. A field with no evidence
stays empty — never hallucinated, never defaulted. Extraction is
rule-based (no LLM call in v1): deterministic, auditable, and cheap.

Fetching reuses `ventoryn.site_snapshot.fetch_page` (SSRF-guarded);
storage mirrors `SnapshotStore` (one row per site in the SaasStore).
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ventoryn.analyze_jobs import validate_url
from ventoryn.site_snapshot import fetch_page

__all__ = ["validate_url", "extract_profile", "collect", "BrandDnaStore",
           "brand_dna_store_for", "learn", "start", "render_card"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS saas_brand_dna (
    site_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    data TEXT
);
"""

REFRESH_COOLDOWN_S = 600

_MAX_COLOURS = 8
_MAX_PRODUCTS = 10
_MAX_AUDIENCE = 6
_MAX_TRIGGERS = 8

# Tone labels, each fired only by a marker word/phrase literally present
# in the page's visible copy. The label is a summary of observed words,
# not a judgement — no marker, no label.
_TONE_MARKERS = (
    ("warm", ("welcome", "we love", "happy to help", "friendly",
              "we're here for you", "we are here for you")),
    ("premium", ("luxury", "premium", "exclusive", "handcrafted",
                 "artisan", "bespoke")),
    ("value", ("affordable", "budget-friendly", "budget friendly",
               "save money", "low price", "best value")),
    ("professional", ("trusted", "certified", "established", "expert",
                      "industry-leading", "industry leading")),
    ("playful", ("fun", "quirky", "playful")),
)

# Audience nouns: a "for <group>" phrase only counts when the group is
# one of these — "for you" or "for more" is not an audience.
_AUDIENCE_NOUNS = (
    "small businesses", "small business owners", "parents", "families",
    "students", "developers", "travellers", "travelers", "homeowners",
    "pet owners", "brides", "gamers", "teachers", "freelancers",
    "startups", "restaurants", "couples", "kids", "children", "seniors",
    "athletes", "musicians", "designers", "marketers", "founders",
    "new parents", "busy parents", "first-time buyers", "home buyers",
    "dog owners", "cat owners", "remote workers", "creators",
)

# Buying triggers: literal phrases that have to appear in the copy.
# The key is the canonical label; the value is the phrases that fire it.
_TRIGGERS = (
    ("free shipping", ("free shipping",)),
    ("free trial", ("free trial",)),
    ("money-back guarantee", ("money-back guarantee", "money back guarantee")),
    ("limited time", ("limited time", "limited-time")),
    ("sale", ("sale",)),
    ("free returns", ("free returns",)),
    ("same-day delivery", ("same-day delivery", "same day delivery")),
    ("cancel anytime", ("cancel anytime",)),
    ("no commitment", ("no commitment",)),
    ("free consultation", ("free consultation",)),
)

_HEX_RE = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
_PCT_OFF_RE = re.compile(r"\d+\s*%\s*off")


class _Page(HTMLParser):
    """Home-page parse: head facts, colours, JSON-LD, visible copy."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.lang, self.meta = "", "", {}
        self.style_blocks: list[str] = []
        self.inline_styles: list[str] = []
        self.jsonld_blocks: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.text_chunks: list[str] = []
        self._in_title = False
        self._in_style = False
        self._in_jsonld = False
        self._in_text = False
        self._in_anchor = False
        self._anchor_href = ""
        self._anchor_text = ""
        self._svg_depth = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "svg":
            self._svg_depth += 1
        elif tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "title":
            if self._svg_depth == 0:
                self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and key not in self.meta:
                self.meta[key] = a.get("content", "")
        elif tag == "style":
            self._in_style = True
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld = True
        elif tag in ("h1", "h2", "h3", "p", "li", "blockquote", "td"):
            self._in_text = True
        elif tag == "a" and a.get("href"):
            self._in_anchor = True
            self._anchor_href = a["href"]
            self._anchor_text = ""
        if a.get("style"):
            self.inline_styles.append(a["style"])

    def handle_endtag(self, tag):
        if tag == "svg":
            self._svg_depth = max(0, self._svg_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "style":
            self._in_style = False
        elif tag == "script":
            self._in_jsonld = False
        elif tag in ("h1", "h2", "h3", "p", "li", "blockquote", "td"):
            self._in_text = False
        elif tag == "a" and self._in_anchor:
            self._in_anchor = False
            self.links.append((self._anchor_href, self._anchor_text.strip()))
            self._anchor_href = ""
            self._anchor_text = ""

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data
        elif self._in_style:
            self.style_blocks.append(data)
        elif self._in_jsonld:
            self.jsonld_blocks.append(data)
        elif self._in_anchor:
            self._anchor_text += data
            self.text_chunks.append(data)
        elif self._in_text:
            self.text_chunks.append(data)


def _visible_text(page: _Page) -> str:
    return " ".join(" ".join(page.text_chunks).split())


def _tone(text: str) -> list[str]:
    lowered = text.lower()
    found = []
    for label, markers in _TONE_MARKERS:
        # Word-boundary match: "fun" must not fire on "refund".
        if any(re.search(r"\b" + re.escape(m) + r"\b", lowered)
               for m in markers):
            found.append(label)
    # Three or more exclamation marks in the copy reads playful too.
    if "playful" not in found and lowered.count("!") >= 3:
        found.append("playful")
    return found


def _colours(page: _Page) -> list[str]:
    seen: list[str] = []

    def add(source: str) -> None:
        for match in _HEX_RE.finditer(source or ""):
            raw = match.group(1).lower()
            if len(raw) == 3:
                raw = "".join(c * 2 for c in raw)
            colour = "#" + raw
            if colour not in seen:
                seen.append(colour)
            if len(seen) >= _MAX_COLOURS:
                return

    add(page.meta.get("theme-color", ""))
    for block in page.style_blocks:
        add(block)
        if len(seen) >= _MAX_COLOURS:
            return seen
    for style in page.inline_styles:
        add(style)
        if len(seen) >= _MAX_COLOURS:
            return seen
    return seen


def _jsonld_products(blocks: list[str]) -> list[dict]:
    names: list[str] = []
    for block in blocks:
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("@type") == "Product" and node.get("name"):
                    names.append(str(node["name"]).strip())
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return [{"name": n} for n in names if n]


def _products(page: _Page, origin: str) -> list[dict]:
    seen: list[str] = []
    out: list[dict] = []

    def add(name: str, url: str = "") -> None:
        name = " ".join(str(name or "").split())
        if not name or len(name) > 120:
            return
        key = name.lower()
        if key in seen:
            return
        seen.append(key)
        out.append({"name": name, "url": url})

    for item in _jsonld_products(page.jsonld_blocks):
        add(item["name"])
    for href, text in page.links:
        if len(out) >= _MAX_PRODUCTS:
            break
        path = urlsplit(urljoin(origin + "/", href)).path.lower()
        if not any(path.startswith(p) for p in
                   ("/product/", "/products/", "/shop/", "/store/")):
            continue
        add(text, urljoin(origin + "/", href))
    return out[:_MAX_PRODUCTS]


def _audience(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    for noun in _AUDIENCE_NOUNS:
        if re.search(r"\bfor (?:the |your )?" + re.escape(noun) + r"\b",
                     lowered):
            found.append(noun)
        if len(found) >= _MAX_AUDIENCE:
            break
    return found


def _buying_triggers(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    for label, phrases in _TRIGGERS:
        # Word-boundary match: "sale" must not fire on "wholesale".
        if any(re.search(r"\b" + re.escape(p) + r"\b", lowered)
               for p in phrases):
            found.append(label)
        if len(found) >= _MAX_TRIGGERS:
            return found
    if _PCT_OFF_RE.search(lowered) and "% off" not in found:
        found.append("% off")
    return found[:_MAX_TRIGGERS]


def extract_profile(url: str, body: str, final_url: str | None = None) -> dict:
    """Brand profile from one fetched home page. Pure: no network.

    Fields with no evidence stay empty — never hallucinated."""
    page = _Page()
    try:
        page.feed(body or "")
    except Exception:  # noqa: BLE001 - malformed HTML still yields what parsed
        pass
    final = final_url or url
    parts = urlsplit(final)
    origin = f"{parts.scheme}://{parts.netloc}"
    text = _visible_text(page)
    description = (page.meta.get("description")
                   or page.meta.get("og:description", "")).strip()
    return {
        "url": final,
        "host": (parts.hostname or "").lower(),
        "name": (page.meta.get("og:site_name", "").strip()
                 or page.title.strip()),
        "tagline": description[:300],
        "language": page.lang or "",
        "tone": _tone(text),
        "colours": _colours(page),
        "products": _products(page, origin),
        "audience": _audience(text),
        "buying_triggers": _buying_triggers(text),
        "reachable": True,
        "error": None,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def collect(site: dict, *, page_fetch=fetch_page) -> dict:
    """Fetch the site's home page and extract its brand profile.

    `page_fetch` is injected (the snapshot's `fetch_page` by default);
    tests pass a fake and never touch the network."""
    page = page_fetch(site["url"])
    status = page.get("status", 0)
    if not status or status >= 400:
        return {
            "url": page.get("final_url") or site["url"],
            "host": site.get("host", ""),
            "name": "", "tagline": "", "language": "",
            "tone": [], "colours": [], "products": [],
            "audience": [], "buying_triggers": [],
            "reachable": False,
            "error": page.get("error") or f"HTTP {status}",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
    return extract_profile(site["url"], page.get("body", ""),
                           final_url=page.get("final_url"))


# -- storage + background run (mirrors SnapshotStore) --------------------------

class BrandDnaStore:
    def __init__(self, saas_store):
        self._conn, self._lock = saas_store._conn, saas_store._lock
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def get(self, site_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, started_at, finished_at, data FROM saas_brand_dna "
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
                "SELECT status, started_at, finished_at FROM saas_brand_dna "
                "WHERE site_id = ?", (site_id,)).fetchone()
            if row is not None:
                if row[0] == "running" and (
                        now - datetime.fromisoformat(row[1])).total_seconds() < 300:
                    return False
                if row[2] and (now - datetime.fromisoformat(row[2])
                               ).total_seconds() < REFRESH_COOLDOWN_S:
                    return False
            self._conn.execute(
                "INSERT INTO saas_brand_dna (site_id, status, started_at) VALUES (?, 'running', ?) "
                "ON CONFLICT(site_id) DO UPDATE SET status = 'running', started_at = excluded.started_at",
                (site_id, now.isoformat()))
        return True

    def finish(self, site_id: str, status: str, data: dict | None) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE saas_brand_dna SET status = ?, finished_at = ?, "
                    "data = COALESCE(?, data) WHERE site_id = ?",
                    (status, datetime.now(timezone.utc).isoformat(),
                     json.dumps(data) if data is not None else None, site_id))
        except sqlite3.ProgrammingError:
            # Same as the snapshot store: the store was closed while this
            # background learn was still running. A brand profile is a
            # disposable refresh — the cooldown self-heals a row stuck at
            # "running", so dropping this result is a correct no-op.
            pass


def brand_dna_store_for(saas_store) -> BrandDnaStore:
    s = getattr(saas_store, "_ventoryn_brand_dna", None)
    if s is None:
        s = BrandDnaStore(saas_store)
        saas_store._ventoryn_brand_dna = s
    return s


def _store_result(dna: BrandDnaStore, site_id: str, profile: dict) -> None:
    if profile.get("reachable"):
        dna.finish(site_id, "done", profile)
    else:
        dna.finish(site_id, "failed",
                   {"error": profile.get("error") or "could not fetch the site"})


def learn(saas_store, site: dict, *, page_fetch=None) -> dict | None:
    """Learn the brand now, in this thread. None when a learn is already
    running or the refresh cooldown applies."""
    dna = brand_dna_store_for(saas_store)
    if not dna.mark_running(site["id"]):
        return None
    try:
        profile = collect(site, page_fetch=page_fetch or fetch_page)
        _store_result(dna, site["id"], profile)
        return profile
    except Exception as exc:  # noqa: BLE001 - recorded, page shows it
        dna.finish(site["id"], "failed", {"error": str(exc)[:300]})
        return None


def start(saas_store, site: dict, *, collector=None) -> bool:
    """Learn the brand in the background. False when one is already
    running or the refresh cooldown applies."""
    dna = brand_dna_store_for(saas_store)
    if not dna.mark_running(site["id"]):
        return False

    def _run():
        try:
            run = collector or collect
            _store_result(dna, site["id"], run(site))
        except Exception as exc:  # noqa: BLE001 - recorded, page shows it
            dna.finish(site["id"], "failed", {"error": str(exc)[:300]})

    threading.Thread(target=_run, daemon=True,
                     name=f"brand-dna-{site['id']}").start()
    return True


# -- rendering -----------------------------------------------------------------

def _field(label: str, value: str) -> str:
    return (f"<li><span>{html.escape(label)}</span>"
            f"<span class='v'>{value}</span></li>")


def render_card(site: dict, record: dict | None) -> str:
    """The Brand DNA card for the site page."""
    sid = html.escape(site["id"])
    refresh = (f"<form method='post' action='/websites/sites/{sid}/brand-dna' "
               f"style='margin:0'><button class='btn' type='submit'>"
               f"Re-learn brand DNA</button></form>")
    head = ("<div class='card' id='brand-dna'><h2>Brand DNA</h2>")
    if record is None or (record["status"] == "running"
                          and not record.get("data")):
        return (f"{head}<p class='muted'>Learning your brand from your "
                f"website — tone, colours, products, audience and what "
                f"makes buyers act. This takes a few seconds.</p>"
                f"<script>setTimeout(function(){{location.reload()}},6000)"
                f"</script></div>")
    data = record.get("data") or {}
    if record["status"] == "failed" or data.get("error"):
        return (f"{head}<p class='muted'>Brand DNA could not be learned "
                f"({html.escape(data.get('error') or 'unknown error')}).</p>"
                f"{refresh}</div>")
    missing = "<span class='muted'>Not found on the site yet</span>"
    rows = "".join([
        _field("Name", html.escape(data.get("name") or "") or missing),
        _field("Tagline", html.escape(data.get("tagline") or "") or missing),
        _field("Tone of voice",
               html.escape(", ".join(data.get("tone") or [])) or missing),
        _field("Colours",
               html.escape(", ".join(data.get("colours") or [])) or missing),
        _field("Products",
               html.escape(", ".join(p.get("name", "")
                                     for p in data.get("products") or []))
               or missing),
        _field("Audience",
               html.escape(", ".join(data.get("audience") or [])) or missing),
        _field("Buying triggers",
               html.escape(", ".join(data.get("buying_triggers") or []))
               or missing),
    ])
    when = html.escape((data.get("collected_at") or "")[:16].replace("T", " "))
    running = " · refreshing…" if record["status"] == "running" else ""
    return (f"{head}<p class='muted'>Learned from {html.escape(site['host'])}"
            f" · {when} UTC{running}</p>"
            f"<ul class='v-snap-list'>{rows}</ul>"
            f"<p class='muted' style='margin-top:10px'>Only what is actually "
            f"on the site is shown — a field with no evidence stays empty, "
            f"never guessed. The profile shapes the drafts your agents "
            f"write; nothing is published without your approval.</p>"
            f"{refresh}</div>")

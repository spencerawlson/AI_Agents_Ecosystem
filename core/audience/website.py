"""Website intelligence: what a business sells, to whom, where — with
the evidence for every claim.

``collect(url, fetch=...)`` reads the home page and up to seven more
pages of the same site that say the most about the business (pricing,
products, services, courses, about, FAQ), honouring robots.txt for the
extra pages. ``extract`` turns each page into claims:

    {"id", "field", "value", "kind", "confidence",
     "evidence": [{"url", "where", "quote"}], "basis": []}

``kind`` is one of:
- ``fact``       read from the page (structured data, meta tags,
                 headings, visible text) — the quote is the proof;
- ``derived``    computed from facts (frequent terms across pages);
- ``hypothesis`` a guess from a weak signal (a country TLD, a currency
                 symbol) — shown as a guess, never as a fact.

Nothing here calls a model or a paid API; the fetch function is
injected (the SaaS passes its SSRF-guarded fetch). Everything read is
untrusted and passed through ``untrusted.clean``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from core.audience.untrusted import clean

MAX_PAGES = 8
MAX_CLAIMS_PER_FIELD = 12
_SKIP_EXT = re.compile(
    r"\.(pdf|jpe?g|png|gif|webp|svg|ico|css|js|xml|zip|mp4|mp3|woff2?)$", re.I)
_SKIP_PATH = re.compile(
    r"/(login|log-in|signin|sign-in|signup|sign-up|register|account|cart|"
    r"checkout|basket|wp-admin|admin|search|tag|tags|author|feed)(/|$)", re.I)

#: Which extra pages to read first: (score, words in the path or link text).
_PAGE_PRIORITY = (
    (5, ("pricing", "price", "prices", "plans", "plan", "tarifs", "preise",
         "precios", "subscribe", "membership")),
    (4, ("product", "products", "services", "service", "courses", "course",
         "shop", "store", "features", "solutions", "programs", "catalog",
         "menu", "offers", "certification", "training")),
    (3, ("about", "about-us", "company", "who-we-are", "story", "mission",
         "a-propos", "uber-uns")),
    (2, ("faq", "faqs", "help", "how-it-works", "questions")),
    (1, ("blog", "guides", "resources", "learn")),
)

_STRUCTURED_OFFERINGS = ("Product", "Service", "Course", "SoftwareApplication",
                         "MobileApplication", "WebApplication", "Event",
                         "Book", "Menu", "Offer", "EducationalOccupationalProgram")
_STRUCTURED_ORGS = ("Organization", "LocalBusiness", "Corporation",
                    "OnlineBusiness", "OnlineStore", "Store",
                    "EducationalOrganization", "ProfessionalService")

_CURRENCY_SYMBOLS = {"$": "USD?", "US$": "USD", "CA$": "CAD", "C$": "CAD",
                     "A$": "AUD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
_PRICE = re.compile(
    r"(?P<sym>US\$|CA\$|C\$|A\$|[$€£¥₹])\s?(?P<amt>\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)"
    r"|(?P<amt2>\d{1,6}(?:[.,]\d{1,2})?)\s?(?P<code>USD|EUR|GBP|CAD|AUD|€|£)\b")
_PERIOD = re.compile(
    r"^\s*(?:/|per\s+|a\s+|par\s+)?\s*(month|mo|year|yr|week|day|hour|hr|"
    r"user|seat|lesson|session|mois|an)\b", re.I)
_AUDIENCE = re.compile(
    r"\b(?:built|made|designed|perfect|ideal|created|tailored|great)\s+for\s+"
    r"((?:[a-z][a-z'\-]*\s?){1,5}?)(?=[.,;:!?)]|\s+(?:who|that|and|to|in|with|"
    r"like|of)\b|$)", re.I)

#: Headings that belong to the page furniture, not the business.
_BOILERPLATE = re.compile(
    r"cookie|consent|privacy|gdpr|newsletter|subscribe to|sign up for our|"
    r"follow us|all rights reserved|skip to|table of contents", re.I)


def _business_heading(text: str) -> bool:
    return len(text.split()) >= 3 and not _BOILERPLATE.search(text)


_CC_TLDS = {"ca": "Canada", "uk": "United Kingdom", "au": "Australia",
            "nz": "New Zealand", "ie": "Ireland", "fr": "France",
            "de": "Germany", "es": "Spain", "it": "Italy", "nl": "Netherlands",
            "be": "Belgium", "ch": "Switzerland", "at": "Austria",
            "se": "Sweden", "no": "Norway", "dk": "Denmark", "fi": "Finland",
            "pt": "Portugal", "br": "Brazil", "mx": "Mexico", "in": "India",
            "jp": "Japan", "sg": "Singapore", "za": "South Africa",
            "ng": "Nigeria", "ke": "Kenya", "pl": "Poland"}

_STOP = set("""a an the and or but of for to in on at by with from your you
our we us is are be was were it its this that these those as can will
all any more most not no yes do does how what why when where who which
get got have has had i me my they them their there here about into over
than then so if up out new now just only also very per vs page home
welcome menu login sign free best one two three more next back skip
content main contact privacy terms policy cookies blog first way ways
every need needs start starting started today ready make made help know
see try use using like want time times pricing price prices plan plans
faq faqs frequently asked question questions answer answers learn
closer think things thing go going take""".split())


class _Doc(HTMLParser):
    """One page: head facts, JSON-LD, headings, text blocks, links."""

    _BLOCK = ("h1", "h2", "h3", "h4", "p", "li", "td", "dd", "blockquote",
              "figcaption", "summary")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.lang = "", ""
        self.meta: dict[str, str] = {}
        self.hreflang: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self.blocks: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._in_title = self._in_jsonld = False
        self._block: list[str] | None = None
        self._block_tag = ""
        self._anchor: list[str] | None = None
        self._anchor_href = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "svg", "template"):
            if tag == "script" and "ld+json" in a.get("type", "").lower():
                self._in_jsonld = True
                self.jsonld.append("")
            else:
                self._skip += 1
            return
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and key not in self.meta:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and "alternate" in a.get("rel", "").lower() and a.get("hreflang"):
            self.hreflang.append((a["hreflang"], a.get("href", "")))
        elif tag in self._BLOCK and self._block is None:
            self._block, self._block_tag = [], tag
        elif tag == "a" and a.get("href"):
            self._anchor, self._anchor_href = [], a["href"]

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "template"):
            if self._in_jsonld and tag == "script":
                self._in_jsonld = False
            elif self._skip:
                self._skip -= 1
            return
        if tag == "title":
            self._in_title = False
        elif tag == self._block_tag and self._block is not None:
            text = clean(" ".join(self._block), 600)
            if text:
                self.blocks.append(text)
                if tag in ("h1", "h2", "h3", "h4"):
                    self.headings.append((tag, text))
            self._block, self._block_tag = None, ""
        elif tag == "a" and self._anchor is not None:
            self.links.append((self._anchor_href, clean(" ".join(self._anchor), 120)))
            self._anchor = None

    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld[-1] += data
            return
        if self._skip:
            return
        if self._in_title and len(self.title) < 300:
            self.title += data
        if self._block is not None:
            self._block.append(data)
        if self._anchor is not None:
            self._anchor.append(data)


def parse(body: str) -> _Doc:
    doc = _Doc()
    try:
        doc.feed(body or "")
        doc.close()
    except Exception:  # noqa: BLE001 - a broken page yields what was read
        pass
    doc.title = clean(doc.title, 200)
    return doc


# -- page selection ------------------------------------------------------

def _bare(host: str) -> str:
    host = (host or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _normal(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", "", ""))


def page_score(url: str, text: str) -> int:
    words = set(re.split(r"[^a-z0-9]+", (urlsplit(url).path + " " + text).lower()))
    for score, keys in _PAGE_PRIORITY:
        if words & set(keys):
            return score
    return 0


def select_pages(home_url: str, doc: _Doc, limit: int = MAX_PAGES - 1) -> list[str]:
    """Same-site links worth reading, best first (score, then order)."""
    host = _bare(urlsplit(home_url).hostname or "")
    home = _normal(home_url)
    seen, ranked = {home}, []
    for i, (href, text) in enumerate(doc.links):
        url = urljoin(home_url, href.strip())
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or _bare(p.hostname or "") != host:
            continue
        if _SKIP_EXT.search(p.path) or _SKIP_PATH.search(p.path):
            continue
        norm = _normal(url)
        if norm in seen:
            continue
        seen.add(norm)
        score = page_score(norm, text)
        if score:
            ranked.append((-score, i, norm))
    ranked.sort()
    return [u for _, _, u in ranked[:limit]]


# -- claims --------------------------------------------------------------

def _claim(field, value, kind, confidence, url, where, quote, basis=None):
    return {"field": field, "value": clean(value, 200), "kind": kind,
            "confidence": confidence,
            "evidence": [{"url": url, "where": where, "quote": clean(quote, 240)}],
            "basis": list(basis or [])}


def _jsonld_nodes(doc: _Doc):
    for block in doc.jsonld:
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    return {str(x) for x in (t if isinstance(t, list) else [t]) if x}


def _quote_around(text: str, start: int, end: int, pad: int = 80) -> str:
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


def _prices(url: str, doc: _Doc) -> list[dict]:
    out = []
    for text in doc.blocks:
        for m in _PRICE.finditer(text):
            sym = m.group("sym") or m.group("code") or ""
            amt = m.group("amt") or m.group("amt2") or ""
            if not amt or amt in ("0", "00"):
                continue
            period = _PERIOD.match(text[m.end():])
            value = (f"{sym}{amt}" if m.group("sym") else f"{amt} {sym}")
            if period:
                value += f" / {period.group(1).lower()}"
            out.append(_claim("price", value, "fact", "medium", url, "text",
                              _quote_around(text, m.start(), m.end())))
    for node in _jsonld_nodes(doc):
        if "Offer" in _types(node) and node.get("price") not in (None, ""):
            value = f"{node.get('price')} {node.get('priceCurrency', '')}".strip()
            out.append(_claim("price", value, "fact", "high", url,
                              "structured data (Offer)", json.dumps(
                                  {k: node.get(k) for k in ("name", "price", "priceCurrency")
                                   if node.get(k) is not None})))
    return out


def extract(url: str, doc: _Doc, *, is_home: bool) -> list[dict]:
    """Claims from one page."""
    claims: list[dict] = []
    if is_home:
        name = doc.meta.get("og:site_name", "")
        if name:
            claims.append(_claim("name", name, "fact", "high", url,
                                 "meta og:site_name", name))
        desc = doc.meta.get("description") or doc.meta.get("og:description", "")
        if desc:
            claims.append(_claim("description", desc, "fact", "high", url,
                                 "meta description", desc))
        if doc.lang:
            claims.append(_claim("language", doc.lang.split("-")[0].lower(), "fact",
                                 "high", url, "html lang", f'<html lang="{doc.lang}">'))
        for lang, href in doc.hreflang:
            if lang.lower() != "x-default":
                claims.append(_claim("language", lang.split("-")[0].lower(), "fact",
                                     "high", url, "hreflang alternate",
                                     f'hreflang="{lang}" {href}'))
        for tag, text in doc.headings:
            if tag in ("h1", "h2") and len(text) <= 160 and _business_heading(text):
                claims.append(_claim("value_proposition", text, "fact", "medium",
                                     url, tag, text))
    for node in _jsonld_nodes(doc):
        types = _types(node)
        name = node.get("name")
        if isinstance(name, str) and name.strip():
            if types & set(_STRUCTURED_ORGS) and is_home:
                claims.append(_claim("name", name, "fact", "high", url,
                                     f"structured data ({'/'.join(sorted(types))})", name))
            elif types & set(_STRUCTURED_OFFERINGS) - {"Offer"}:
                claims.append(_claim("offering", name, "fact", "high", url,
                                     f"structured data ({'/'.join(sorted(types))})", name))
        area = node.get("areaServed")
        for item in (area if isinstance(area, list) else [area]):
            label = item.get("name") if isinstance(item, dict) else item
            if isinstance(label, str) and label.strip():
                claims.append(_claim("area_served", label, "fact", "high", url,
                                     "structured data (areaServed)", label))
        addr = node.get("address")
        if isinstance(addr, dict):
            where = ", ".join(str(addr[k]) for k in ("addressLocality", "addressRegion",
                                                     "addressCountry") if addr.get(k))
            if where:
                claims.append(_claim("location", where, "fact", "high", url,
                                     "structured data (address)", where))
    for href, text in doc.links:
        path = urlsplit(urljoin(url, href)).path.lower()
        if (re.match(r"^/(products?|shop|store|courses?|services?|programs?)/[^/]+", path)
                and 2 < len(text) <= 80):
            claims.append(_claim("offering", text, "fact", "medium", url,
                                 "link to an offering page", f"{text} → {path}"))
    claims += _prices(url, doc)
    for text in doc.blocks:
        for m in _AUDIENCE.finditer(text):
            who = m.group(1).strip()
            if 2 < len(who) <= 60 and who.lower().split()[0] not in _STOP:
                claims.append(_claim("audience", who, "fact", "medium", url, "text",
                                     _quote_around(text, m.start(), m.end())))
    return claims


def _keywords(pages: list[tuple[str, _Doc]], limit: int = 15) -> list[dict]:
    """Terms that recur across titles and headings (derived)."""
    counts: Counter = Counter()
    where: dict[str, list[tuple[str, str]]] = {}
    for url, doc in pages:
        texts = [doc.title] + [t for _, t in doc.headings if not _BOILERPLATE.search(t)]
        seen_here = set()
        for text in texts:
            words = [w for w in re.findall(r"[^\W_][\w+#.\-]*(?:[^\W_]|[+#])|[^\W_]",
                                           text.lower()) if w not in _STOP and len(w) > 2]
            grams = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
            for g in grams:
                if g not in seen_here:
                    seen_here.add(g)
                    counts[g] += 1
                    where.setdefault(g, []).append((url, text))
    # A single word that only ever appears inside one recurring pair
    # ("road" in "road cissp") adds nothing: keep the pair.
    subsumed = {w for g, n in counts.items() if " " in g for w in g.split()
                if counts.get(w) == n}
    out = []
    for term, n in counts.most_common():
        if n < 2:
            break
        if term in subsumed:
            continue
        ev = [{"url": u, "where": "title/heading", "quote": clean(q, 240)}
              for u, q in where[term][:3]]
        out.append({"field": "keyword", "value": term, "kind": "derived",
                    "confidence": "medium", "evidence": ev, "basis": []})
        if len(out) >= limit:
            break
    return out


def _geo_hypotheses(home_url: str, claims: list[dict]) -> list[dict]:
    out = []
    host = urlsplit(home_url).hostname or ""
    tld = host.rsplit(".", 1)[-1].lower()
    if tld in _CC_TLDS:
        out.append(_claim("market", _CC_TLDS[tld], "hypothesis", "low", home_url,
                          "country domain", f".{tld} domain: {host}"))
    for c in claims:
        if c["field"] == "price":
            for sym, code in _CURRENCY_SYMBOLS.items():
                if c["value"].startswith(sym) and code != "USD?":
                    out.append({**_claim("currency", code, "hypothesis", "low",
                                         c["evidence"][0]["url"], "price symbol",
                                         c["evidence"][0]["quote"])})
                    break
    return out


def _dedupe(claims: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    per_field: Counter = Counter()
    for c in claims:
        if not c["value"]:
            continue
        key = (c["field"], c["value"].lower())
        if key in merged:
            have = merged[key]
            urls = {e["url"] for e in have["evidence"]}
            for e in c["evidence"]:
                if e["url"] not in urls and len(have["evidence"]) < 3:
                    have["evidence"].append(e)
            continue
        if per_field[c["field"]] >= MAX_CLAIMS_PER_FIELD:
            continue
        per_field[c["field"]] += 1
        merged[key] = c
    out = list(merged.values())
    for i, c in enumerate(out, 1):
        c["id"] = f"c{i}"
    return out


def _robots(origin: str, fetch: Callable[[str], dict]) -> RobotFileParser | None:
    try:
        r = fetch(origin + "/robots.txt")
    except Exception:  # noqa: BLE001
        return None
    if r.get("status") != 200:
        return None
    rp = RobotFileParser()
    rp.parse((r.get("body") or "").splitlines())
    return rp


def collect(url: str, *, fetch: Callable[[str], dict], max_pages: int = MAX_PAGES,
            user_agent: str = "AxionoryBot") -> dict:
    """Read the site and return {"url", "pages", "claims", "skipped"}.

    ``fetch(url) -> {"status", "final_url", "body", ...}`` must be an
    SSRF-guarded fetch. A page that cannot be read is listed with its
    status; it never stops the others."""
    parts = urlsplit(url)
    if not parts.path:
        url = urlunsplit((parts.scheme, parts.netloc, "/", parts.query, ""))
    home = fetch(url)
    final = home.get("final_url") or url
    pages_meta = [{"url": final, "status": home.get("status", 0)}]
    if home.get("status") != 200 or not home.get("body"):
        return {"url": final, "pages": pages_meta, "claims": [], "skipped": [],
                "error": home.get("error") or f"home page returned {home.get('status', 0)}"}
    docs = [(final, parse(home["body"]))]
    pages_meta[0]["title"] = docs[0][1].title
    p = urlsplit(final)
    robots = _robots(f"{p.scheme}://{p.netloc}", fetch)
    skipped = []
    for link in select_pages(final, docs[0][1], limit=max(0, max_pages - 1)):
        if robots is not None and not robots.can_fetch(user_agent, link):
            skipped.append({"url": link, "reason": "robots.txt disallows it"})
            continue
        r = fetch(link)
        meta = {"url": r.get("final_url") or link, "status": r.get("status", 0)}
        if r.get("status") == 200 and r.get("body"):
            host_ok = _bare(urlsplit(meta["url"]).hostname or "") == _bare(p.hostname or "")
            if host_ok:
                doc = parse(r["body"])
                meta["title"] = doc.title
                docs.append((meta["url"], doc))
            else:
                skipped.append({"url": link, "reason": "redirected off the site"})
        pages_meta.append(meta)
    claims: list[dict] = []
    for i, (page_url, doc) in enumerate(docs):
        claims += extract(page_url, doc, is_home=(i == 0))
    claims += _keywords(docs)
    claims += _geo_hypotheses(final, claims)
    return {"url": final, "pages": pages_meta, "claims": _dedupe(claims),
            "skipped": skipped, "error": None, "texts": {
                u: [d.title] + d.blocks[:60] for u, d in docs}}

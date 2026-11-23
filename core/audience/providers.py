"""Discovery sources: one interface, each source honest about itself.

Every source declares what it needs and what it may do
(``ProviderInfo``): authentication, the operation it performs, its rate
limit, its cost, how long its results may be kept, and whether its
terms allow this commercial use. ``availability(env)`` says whether it
can run on this server right now and, when not, why — and the report
shows that reason instead of quietly skipping the source.

Live sources (official APIs, read-only, search only):
- ``brave``          Brave Search API: web results plus forum
                     "discussions". Needs BRAVE_SEARCH_API_KEY.
- ``stackexchange``  Stack Exchange API: questions on the Q&A sites
                     whose topic matches the business. No key needed
                     (STACKEXCHANGE_KEY raises the daily quota).
- ``hackernews``     Hacker News search (Algolia's public API).
- ``youtube``        YouTube Data API search. Needs YOUTUBE_API_KEY.

Listed but not used, with the reason (so the gap is visible):
Reddit, X, Facebook, Instagram, Threads, LinkedIn, TikTok, Pinterest,
Google Search, Bing.

Evidence items never carry an author or username: discovery is about
topics and communities, not people. Every text field is untrusted and
cleaned. ``http(url, headers) -> (status, json | None)`` is injected
(the SaaS passes its SSRF-guarded fetch; tests pass fakes).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlencode

from core.audience.untrusted import clean

Http = Callable[[str, dict], tuple[int, object]]

#: Words too general to say what a community is about.
_GENERIC = set("""the and for with you your our who what how best new and
users people professionals enthusiasts experts developers questions answers
site community learn learning beginners advanced general help repair
repairs maintenance service services home business businesses online
local professional company free app apps tool tools software""".split())

#: Operator kill switch: comma-separated provider names to turn off.
DISABLE_ENV = "AUDIENCE_PROVIDERS_DISABLED"


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    label: str
    kind: str                 # search | community | video | social
    auth: str                 # what it needs, in words
    env: tuple[str, ...]      # env vars it reads (required ones first)
    operations: str           # what it does — always read-only search here
    rate_limit: str           # in words
    min_interval_s: float     # pause between two calls to it
    max_queries: int          # per scan
    cost_note: str
    cost_env: str = ""        # env var holding the USD cost per call, if any
    retention_days: int = 30  # how long its results are kept
    terms: str = ""           # where the terms are
    commercial_use: bool = True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def evidence(*, source: str, venue: str, url: str, title: str, snippet: str,
             published_at: str = "", engagement: dict | None = None,
             kind: str = "page") -> dict:
    """One normalized discovery result. No author fields, by design."""
    return {"source": source, "venue": clean(venue, 80), "url": url[:500],
            "title": clean(title, 200), "snippet": clean(snippet, 400),
            "published_at": published_at, "kind": kind,
            "engagement": {k: int(v) for k, v in (engagement or {}).items()
                           if isinstance(v, (int, float))}}


class Provider:
    info: ProviderInfo

    def availability(self, env) -> tuple[bool, str]:
        disabled = {p.strip() for p in (env.get(DISABLE_ENV) or "").split(",")}
        if self.info.name in disabled:
            return False, "turned off by the operator"
        missing = [k for k in self.required_env() if not env.get(k)]
        if missing:
            return False, f"not configured (needs {', '.join(missing)})"
        return True, ""

    def required_env(self) -> tuple[str, ...]:
        return ()

    def cost_per_call_usd(self, env) -> float | None:
        """None when the cost is not known on this server."""
        if not self.info.cost_env:
            return 0.0
        try:
            return float(env.get(self.info.cost_env) or "")
        except ValueError:
            return None

    def search(self, query: dict, http: Http, env) -> list[dict]:
        raise NotImplementedError


class BraveSearch(Provider):
    info = ProviderInfo(
        name="brave", label="Brave Search", kind="search",
        auth="API key (X-Subscription-Token)", env=("BRAVE_SEARCH_API_KEY",),
        operations="web and discussion search, read-only",
        rate_limit="plan-dependent; Axionory waits 1.1 s between calls",
        min_interval_s=1.1, max_queries=8,
        cost_note="billed per query on your Brave plan; set "
                  "BRAVE_COST_PER_QUERY_USD so it is counted",
        cost_env="BRAVE_COST_PER_QUERY_USD", retention_days=30,
        terms="https://api-dashboard.search.brave.com/terms-of-service")

    def required_env(self):
        return ("BRAVE_SEARCH_API_KEY",)

    def search(self, query, http, env):
        params = {"q": query["text"], "count": 20,
                  "result_filter": "discussions,web", "freshness": "py"}
        if query.get("country"):
            params["country"] = query["country"].upper()[:2]
        if query.get("language"):
            params["search_lang"] = query["language"].lower()[:2]
        status, data = http("https://api.search.brave.com/res/v1/web/search?"
                            + urlencode(params),
                            {"X-Subscription-Token": env["BRAVE_SEARCH_API_KEY"],
                             "Accept": "application/json"})
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"Brave Search returned {status}")
        out = []
        for r in ((data.get("discussions") or {}).get("results") or []):
            d = r.get("data") or {}
            out.append(evidence(
                source="brave", kind="discussion",
                venue=d.get("forum_name") or _host(r.get("url", "")),
                url=r.get("url", ""), title=d.get("question") or r.get("title", ""),
                snippet=d.get("top_comment") or r.get("description", ""),
                published_at=r.get("page_age", ""),
                engagement={"answers": _int(d.get("num_answers"))}))
        for r in ((data.get("web") or {}).get("results") or []):
            out.append(evidence(
                source="brave", kind="page", venue=_host(r.get("url", "")),
                url=r.get("url", ""), title=r.get("title", ""),
                snippet=r.get("description", ""), published_at=r.get("page_age", "")))
        return [e for e in out if e["url"].startswith(("http://", "https://"))]


class StackExchange(Provider):
    info = ProviderInfo(
        name="stackexchange", label="Stack Exchange", kind="community",
        auth="none (optional key raises the quota)", env=("STACKEXCHANGE_KEY",),
        operations="question search on matching Q&A sites, read-only",
        rate_limit="300 calls/day without a key, 10,000 with one; honours "
                   "the API's backoff",
        min_interval_s=0.5, max_queries=6, cost_note="free",
        retention_days=30, terms="https://stackoverflow.com/legal/api-terms-of-use")

    _SITES_CACHE: dict = {}

    def sites(self, http, env) -> list[dict]:
        if "sites" not in self._SITES_CACHE:
            params = {"pagesize": 400, "filter": "default"}
            if env.get("STACKEXCHANGE_KEY"):
                params["key"] = env["STACKEXCHANGE_KEY"]
            status, data = http("https://api.stackexchange.com/2.3/sites?"
                                + urlencode(params), {})
            if status != 200 or not isinstance(data, dict):
                raise RuntimeError(f"Stack Exchange returned {status}")
            self._SITES_CACHE["sites"] = [
                {"param": s.get("api_site_parameter", ""),
                 "name": _unescape(s.get("name", "")),
                 "audience": _unescape(s.get("audience", "")), "url": s.get("site_url", "")}
                for s in data.get("items") or []
                if s.get("site_type") == "main_site" and s.get("api_site_parameter")]
        return self._SITES_CACHE["sites"]

    @staticmethod
    def pick_sites(terms: list[str], sites: list[dict], limit: int = 2) -> list[dict]:
        """The Q&A sites whose name or audience shares words with the
        business's terms. None matching → none searched (reported)."""
        from collections import Counter

        freq = Counter(w for t in terms for w in _words(t) if w not in _GENERIC)
        scored = []
        for i, s in enumerate(sites):
            name = set(_words(s["name"])) - _GENERIC
            aud = set(_words(s["audience"])) - _GENERIC
            # The site's own name must share a topic word with the business;
            # words the business uses more often count more.
            if not name & set(freq):
                continue
            score = 3 * sum(freq[w] for w in name & set(freq)) + sum(
                freq[w] for w in aud & set(freq))
            scored.append((-score, i, s))
        scored.sort(key=lambda x: (x[0], x[1]))
        if not scored:
            return []
        best = -scored[0][0]
        return [s for neg, _, s in scored[:limit] if -neg >= 0.6 * best]

    def search(self, query, http, env):
        out = []
        for site in query.get("stackexchange_sites") or []:
            params = {"q": query["text"], "site": site["param"], "order": "desc",
                      "sort": "relevance", "pagesize": 15}
            if env.get("STACKEXCHANGE_KEY"):
                params["key"] = env["STACKEXCHANGE_KEY"]
            status, data = http("https://api.stackexchange.com/2.3/search/excerpts?"
                                + urlencode(params), {})
            if status != 200 or not isinstance(data, dict):
                raise RuntimeError(f"Stack Exchange returned {status}")
            for it in data.get("items") or []:
                if it.get("item_type") != "question" or not it.get("question_id"):
                    continue
                out.append(evidence(
                    source="stackexchange", kind="question", venue=site["name"],
                    url=f"{site['url'].rstrip('/')}/q/{int(it['question_id'])}",
                    title=_unescape(it.get("title", "")),
                    snippet=_unescape(it.get("excerpt", "")),
                    published_at=_iso_from_ts(it.get("creation_date")),
                    engagement={"score": _int(it.get("score")),
                                "answers": _int(it.get("answer_count"))}))
            if data.get("backoff"):
                time.sleep(min(int(data["backoff"]), 30))
        return out


class HackerNews(Provider):
    info = ProviderInfo(
        name="hackernews", label="Hacker News", kind="community",
        auth="none", env=(), operations="story and comment search (Algolia), read-only",
        rate_limit="10,000 calls/hour per IP; Axionory waits 0.5 s",
        min_interval_s=0.5, max_queries=6, cost_note="free", retention_days=30,
        terms="https://hn.algolia.com/api")

    def search(self, query, http, env):
        since = int(time.time()) - 365 * 86400
        params = {"query": query["text"], "tags": "(story,comment)",
                  "numericFilters": f"created_at_i>{since}", "hitsPerPage": 20}
        status, data = http("https://hn.algolia.com/api/v1/search?" + urlencode(params), {})
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"Hacker News search returned {status}")
        out = []
        for h in data.get("hits") or []:
            oid = str(h.get("objectID") or "")
            if not oid.isdigit():
                continue
            is_comment = bool(h.get("comment_text"))
            out.append(evidence(
                source="hackernews", kind="comment" if is_comment else "story",
                venue="Hacker News",
                url=f"https://news.ycombinator.com/item?id={oid}",
                title=h.get("title") or h.get("story_title") or "",
                snippet=_strip_tags(h.get("comment_text") or h.get("story_text") or ""),
                published_at=_iso_from_ts(h.get("created_at_i")),
                engagement={"points": _int(h.get("points")),
                            "comments": _int(h.get("num_comments"))}))
        return out


class YouTube(Provider):
    info = ProviderInfo(
        name="youtube", label="YouTube", kind="video",
        auth="API key", env=("YOUTUBE_API_KEY",),
        operations="video search, read-only",
        rate_limit="10,000 quota units/day; one search costs 100 units",
        min_interval_s=0.5, max_queries=4, cost_note="free within the daily quota",
        retention_days=30,
        terms="https://developers.google.com/youtube/terms/api-services-terms-of-service")

    def required_env(self):
        return ("YOUTUBE_API_KEY",)

    def search(self, query, http, env):
        params = {"part": "snippet", "q": query["text"], "type": "video",
                  "maxResults": 15, "order": "relevance", "key": env["YOUTUBE_API_KEY"],
                  "publishedAfter": datetime.fromtimestamp(
                      time.time() - 365 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if query.get("language"):
            params["relevanceLanguage"] = query["language"][:2]
        if query.get("country"):
            params["regionCode"] = query["country"].upper()[:2]
        status, data = http("https://www.googleapis.com/youtube/v3/search?"
                            + urlencode(params), {})
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"YouTube returned {status}")
        out = []
        for it in data.get("items") or []:
            vid = (it.get("id") or {}).get("videoId") or ""
            sn = it.get("snippet") or {}
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
                continue
            out.append(evidence(
                source="youtube", kind="video", venue="YouTube",
                url=f"https://www.youtube.com/watch?v={vid}",
                title=sn.get("title", ""), snippet=sn.get("description", ""),
                published_at=sn.get("publishedAt", "")))
        return out


class NotUsed(Provider):
    """A source Axionory does not search, with the reason shown."""

    def __init__(self, name, label, kind, reason, commercial_use=True):
        self.info = ProviderInfo(
            name=name, label=label, kind=kind, auth="—", env=(),
            operations="none", rate_limit="—", min_interval_s=0, max_queries=0,
            cost_note="—", retention_days=0, commercial_use=commercial_use)
        self.reason = reason

    def availability(self, env):
        return False, self.reason


def registry() -> list[Provider]:
    return [
        BraveSearch(), StackExchange(), HackerNews(), YouTube(),
        NotUsed("reddit", "Reddit", "community",
                "not used: Reddit's terms require a commercial data agreement "
                "for this use", commercial_use=False),
        NotUsed("x", "X", "social",
                "not enabled: X search is paid per read and needs your approval"),
        NotUsed("facebook", "Facebook", "social",
                "unavailable: no public search of posts; waiting on Meta app review"),
        NotUsed("instagram", "Instagram", "social",
                "unavailable: hashtag search needs Meta app review"),
        NotUsed("threads", "Threads", "social",
                "unavailable: keyword search needs Meta app review"),
        NotUsed("linkedin", "LinkedIn", "social",
                "unavailable: LinkedIn offers no search API for this use",
                commercial_use=False),
        NotUsed("tiktok", "TikTok", "social",
                "unavailable: TikTok's Research API is for academic research only",
                commercial_use=False),
        NotUsed("pinterest", "Pinterest", "social",
                "unavailable: Pinterest Trends access needs approval"),
        NotUsed("google", "Google Search", "search",
                "unavailable: no general web-search API open to new users; "
                "Brave Search covers the web"),
        NotUsed("bing", "Bing", "search", "unavailable: Bing's search API was retired"),
    ]


def server_env() -> dict:
    keys = {DISABLE_ENV}
    for p in registry():
        keys.update(p.info.env)
        if p.info.cost_env:
            keys.add(p.info.cost_env)
    return {k: os.environ.get(k, "") for k in keys}


def _words(text: str) -> list[str]:
    """Lower-case words, with a plain plural reduced (pets -> pet)."""
    out = []
    for w in re.findall(r"[\w+#]{3,}", (text or "").lower()):
        w = w.strip("+#")
        if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            w = w[:-1]
        out.append(w)
    return out


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    host = (m.group(1) if m else "").lower()
    return host[4:] if host.startswith("www.") else host


def _strip_tags(text: str) -> str:
    return _unescape(re.sub(r"<[^>]+>", " ", text or ""))


def _unescape(text: str) -> str:
    import html

    return html.unescape(text or "")

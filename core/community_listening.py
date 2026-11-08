"""Community listening: find real conversations where buyers already are.

HeyCatch's version of this searches Reddit, X and Threads and drafts
replies. The ecosystem's version starts where the evidence actually
reaches, and says so where it does not:

- **Reddit — live, no key.** Public search JSON
  (``/r/<sub>/search.json``), read-only, no account. It works from a
  residential IP (FedServ — the same reason Google Trends runs there:
  datacenter IPs get Reddit's "blocked due to a network policy" page
  and Trends' 429s). From a blocked network the listener reports
  ``reddit: unavailable`` with the reason and finds zero threads —
  it never invents threads to fill the gap.
- **X and Threads — not live.** There is no public search on either
  without API credentials (X redirects search to login; Threads the
  same). Both are listed in every report as ``unavailable: needs API
  credentials``, never faked. The ``SOURCES`` registry is where a
  bearer-token transport plugs in later.

What the listener does, in order:

1. Search each configured subreddit for the business's keywords
   (``t=week`` — a reply to a stale thread helps nobody).
2. Drop what must not be answered: NSFW, locked, deleted-author,
   older than 14 days, promotional.
3. Score what is left, deterministically, 0–100, with the reasons
   recorded — a question (+35), a stated problem (+25), a decision
   being made (+20), a keyword in the title (+15, body +8), a real
   discussion already (+5), promotional signals (−30). >= 50 is worth
   a reply, >= 75 is high intent. No LLM is involved in scoring, so a
   score is always explainable.
4. Draft replies (creative agent, ``community_replies``) that answer
   first and promote last: the 90/10 rule, enforced by
   :func:`validate_reply_draft` — the site is mentioned at most once,
   only in the last sentences, disclosed as "I built", after at least
   200 characters of genuine help, with no promo language and at most
   one link. Drafts that fail the check are dropped, never posted.
5. Nothing is posted, ever. Drafts are files under
   ``reports/community_drafts/``; the owner posts them by hand and
   marks them done. The daily plan picks drafted replies up as
   ``reply:<thread-id>`` items.

The HTTP layer follows the repo's ``Fetcher`` pattern (see
``core/site_audit.py``): every function takes a ``fetch`` callable
returning ``(status, body)``, so tests inject fake JSON and production
uses :func:`reddit_fetch`.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

USER_AGENT = "Mozilla/5.0 (compatible; EcosystemCommunityListen/1.0)"

# fetch(url) -> (status_code, body_text); status 0 means network failure.
Fetcher = Callable[[str], "tuple[int, str]"]

MAX_AGE = timedelta(days=14)
REPLY_THRESHOLD = 50
HIGH_INTENT = 75

# Sources the listener knows about, and why each is or is not live.
# X / Threads stay here, visibly unavailable, until credentials exist.
SOURCES = {
    "reddit": {
        "status": "live",
        "how": "public search JSON, no key, read-only",
    },
    "x": {
        "status": "unavailable",
        "reason": "no public search without API credentials "
                  "(search redirects to login)",
    },
    "threads": {
        "status": "unavailable",
        "reason": "no public search without API credentials",
    },
}

_QUESTION_OPENERS = ("how", "what", "why", "which", "where", "when", "can",
                     "should", "is", "are", "do", "does", "anyone", "any")
_PAIN_WORDS = ("struggling", "stuck", "failed", "fail", "confused",
               "don't understand", "dont understand", "overwhelmed", "lost",
               "help", "advice", "tips", "recommend", "recommendation")
_DECISION_WORDS = ("best", "worth it", "should i", " vs ", "versus",
                   "which one", "looking for")
_PROMO_WORDS = ("discount", "promo code", "buy now", "check out my",
                "my course", "% off", "giveaway", "affiliate", "use my code")
_PROMO_DRAFT_WORDS = ("buy now", "discount", "promo code", "% off",
                      "limited time", "sign up now", "guaranteed",
                      "100% pass")
_DISCLOSURE_WORDS = ("i built", "i run", "my site", "i made")


class SourceUnavailable(Exception):
    """A community source could not be read. ``source`` names it,
    ``reason`` says why — the mission reports this, never a fake
    thread list."""

    def __init__(self, source: str, reason: str):
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


def reddit_fetch(url: str, timeout: int = 15) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(2_000_000).decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:  # noqa: BLE001 - network failures are data, not crashes
        return 0, ""


# -- parsing -----------------------------------------------------------------

def parse_listing(payload: dict) -> list[dict]:
    """Reddit listing JSON -> thread dicts (only the fields we use)."""
    threads = []
    for child in (payload.get("data") or {}).get("children", []):
        d = child.get("data") or {}
        if not d.get("id") or not d.get("title"):
            continue
        threads.append({
            "id": str(d["id"]),
            "source": "reddit",
            "venue": f"r/{d.get('subreddit', '')}",
            "subreddit": str(d.get("subreddit", "")),
            "title": str(d["title"]),
            "text": str(d.get("selftext", "") or ""),
            "url": "https://www.reddit.com" + str(d.get("permalink", "")),
            "author": str(d.get("author", "")),
            "score": int(d.get("score") or 0),
            "num_comments": int(d.get("num_comments") or 0),
            "created_utc": float(d.get("created_utc") or 0),
            "over_18": bool(d.get("over_18")),
            "locked": bool(d.get("locked")),
        })
    return threads


def search_reddit(subreddit: str, query: str, limit: int = 5,
                  fetch: Fetcher = reddit_fetch) -> list[dict]:
    """Search one subreddit (newest first, past week). Raises
    :class:`SourceUnavailable` instead of returning invented threads."""
    sub = subreddit[2:] if subreddit.startswith("r/") else subreddit
    url = ("https://www.reddit.com/r/" + urllib.parse.quote(sub)
           + "/search.json?" + urllib.parse.urlencode(
               {"q": query, "restrict_sr": "1", "sort": "new",
                "limit": str(limit), "t": "week"}))
    status, body = fetch(url)
    if status == 0:
        raise SourceUnavailable("reddit", "network failure reaching reddit.com")
    if status in (403, 429) or "whoa there, pardner" in body:
        raise SourceUnavailable(
            "reddit",
            "blocked — Reddit refuses datacenter IPs; run listening from "
            "a residential IP (FedServ), like Google Trends")
    if status != 200:
        raise SourceUnavailable("reddit", f"HTTP {status} from reddit.com")
    try:
        payload = json.loads(body)
    except ValueError:
        raise SourceUnavailable("reddit", "unparsable response from reddit.com")
    return parse_listing(payload)


def fetch_subreddit_rules(subreddit: str,
                          fetch: Fetcher = reddit_fetch) -> list[dict]:
    """A subreddit's posted rules, best-effort. Any failure returns []
    — rules inform the draft, they never block listening."""
    sub = subreddit[2:] if subreddit.startswith("r/") else subreddit
    url = ("https://www.reddit.com/r/" + urllib.parse.quote(sub)
           + "/about/rules.json")
    try:
        status, body = fetch(url)
        if status != 200:
            return []
        rules = (json.loads(body).get("data") or {}).get("rules", [])
        return [{"short_name": r.get("short_name", ""),
                 "description": r.get("description", "")}
                for r in rules if isinstance(r, dict)]
    except Exception:  # noqa: BLE001 - rules are advisory, not load-bearing
        return []


# -- scoring -----------------------------------------------------------------

def score_thread(thread: dict, keywords: list[str]) -> tuple[int, list[str]]:
    """Deterministic intent score 0–100, with the reasons. No LLM:
    a score must be explainable from the thread itself."""
    title = (thread.get("title") or "").strip()
    text = ((thread.get("title") or "") + " "
            + (thread.get("text") or "")).lower()
    score = 0
    reasons: list[str] = []

    lowered_title = title.lower()
    if title.endswith("?") or lowered_title.startswith(_QUESTION_OPENERS):
        score += 35
        reasons.append("asks a question")
    if any(w in text for w in _PAIN_WORDS):
        score += 25
        reasons.append("states a problem / asks for help")
    if any(w in text for w in _DECISION_WORDS):
        score += 20
        reasons.append("deciding between options")
    matched = [k for k in keywords if k.lower() in text]
    if matched:
        if any(k.lower() in lowered_title for k in keywords):
            score += 15
            reasons.append(f"keyword in title: {matched[0]}")
        else:
            score += 8
            reasons.append(f"keyword in body: {matched[0]}")
    if (thread.get("num_comments") or 0) >= 3:
        score += 5
        reasons.append("real discussion already")
    if any(w in text for w in _PROMO_WORDS):
        score -= 30
        reasons.append("looks promotional")
    return max(0, min(100, score)), reasons


def intent_label(score: int) -> str:
    if score >= HIGH_INTENT:
        return "high"
    if score >= REPLY_THRESHOLD:
        return "worth_reply"
    return "low"


def filter_threads(threads: list[dict], keywords: list[str],
                   now: datetime) -> tuple[list[dict], list[dict]]:
    """Drop what must not be answered; score and rank the rest.
    Returns (kept, dropped) — dropped items carry their reason, so the
    report can show what was skipped and why."""
    kept, dropped = [], []
    seen: set[str] = set()
    for thread in threads:
        tid = thread["id"]
        if tid in seen:
            dropped.append({**thread, "drop_reason": "duplicate"})
            continue
        seen.add(tid)
        created = datetime.fromtimestamp(thread.get("created_utc") or 0,
                                         tz=timezone.utc)
        if thread.get("over_18"):
            dropped.append({**thread, "drop_reason": "nsfw"})
        elif thread.get("locked"):
            dropped.append({**thread, "drop_reason": "locked"})
        elif (thread.get("author") or "") in ("[deleted]", ""):
            dropped.append({**thread, "drop_reason": "author gone"})
        elif now - created > MAX_AGE:
            dropped.append({**thread, "drop_reason": "older than 14 days"})
        else:
            score, reasons = score_thread(thread, keywords)
            kept.append({**thread, "intent_score": score,
                         "intent": intent_label(score), "reasons": reasons})
    kept.sort(key=lambda t: (-t["intent_score"], -t["created_utc"]))
    return kept, dropped


# -- the 90/10 draft gate ------------------------------------------------------

def validate_reply_draft(text: str, site_url: str) -> list[str]:
    """Deterministic check on a reply draft. Errors block the draft —
    the same shape as the SEO quality gate, applied to promotion.
    The site may be mentioned at most once, only in the last
    sentences, disclosed as the author's own, after at least 200
    characters of genuine help, with no promo language and at most
    one link."""
    errors: list[str] = []
    body = (text or "").strip()
    if not body:
        return ["empty reply"]
    lowered = body.lower()
    domain = urllib.parse.urlparse(site_url).netloc.lower()
    mentions = lowered.count(domain) + lowered.count(site_url.lower())
    if mentions == 0:
        errors.append("does not mention the site (a reply draft must say "
                      "where the help comes from)")
    elif mentions > 1:
        errors.append(f"mentions the site {mentions} times (max 1)")
    if not any(w in lowered for w in _DISCLOSURE_WORDS):
        errors.append("no disclosure — say you built / run the site")
    if any(w in lowered for w in _PROMO_DRAFT_WORDS):
        errors.append("promotional language (buy now / discount / guaranteed)")
    links = len(re.findall(r"https?://[^\s]+", lowered))
    # Bare domains are links too on Reddit (autolinked), but the site's
    # own URL already counted above must not count twice.
    links += len(re.findall(
        r"(?<![\w/:])[\w-]+\.(?:com|net|org|io|ai|me|co|app|dev|site)\b",
        lowered))
    if links > 1:
        errors.append(f"{links} links (max 1)")
    if mentions == 1 and domain:
        # The mention must sit in the last two sentences.
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
        tail = " ".join(sentences[-2:]).lower()
        if domain not in tail and site_url.lower() not in tail:
            errors.append("site mention is not at the end (90/10: help "
                          "first, mention last)")
        head = body.lower().split(domain)[0]
        if len(head.strip()) < 200:
            errors.append("less than 200 characters of help before the "
                          "site mention")
    return errors


def sign_reply_draft(text: str, site_url: str, signer=None) -> tuple[str, str | None]:
    """Add the Ventoryn signature to a VALIDATED reply draft: the
    site URL inside the draft is replaced by its signed form (UTM
    parameters + signed vtn_ tag). `signer` is a callable
    ``(url, agent) -> (signed_url, tag)`` supplied by the SaaS shell
    when the draft belongs to a Ventoryn site (see
    core.signature.sign_agent_link). Without a signer the draft is
    returned unchanged with tag None — and the caller must say so
    (unsigned), never pretend it is signed. Returns (text, tag)."""
    if signer is None:
        return text, None
    signed_url, tag = signer(site_url, "community")
    return text.replace(site_url, signed_url, 1), tag


# -- orchestration -------------------------------------------------------------

def listen_reddit(subreddits: list[str], keywords: list[str],
                  fetch: Fetcher = reddit_fetch,
                  limit_per_query: int = 5, max_queries: int = 10,
                  now: datetime | None = None) -> dict:
    """Search Reddit for the keywords across the subreddits. Returns
    {"threads": [...scored, ranked...], "dropped": [...],
    "sources": {...}}. A blocked Reddit stops further queries and is
    reported in sources — never papered over with invented threads."""
    now = now or datetime.now(timezone.utc)
    # Interleave (subreddit, keyword) pairs so the query budget covers
    # every subreddit before it covers a second keyword anywhere.
    pairs = []
    for i in range(max_queries):
        sub = subreddits[i % len(subreddits)]
        kw = keywords[(i // len(subreddits)) % len(keywords)]
        pairs.append((sub, kw))
    raw: list[dict] = []
    queries = 0
    reddit_status: dict = {"status": "ok", "queries": 0, "raw_found": 0}
    for sub, kw in pairs:
        try:
            found = search_reddit(sub, kw, limit=limit_per_query, fetch=fetch)
        except SourceUnavailable as exc:
            reddit_status = {"status": "unavailable", "reason": exc.reason,
                             "queries": queries, "raw_found": len(raw)}
            break
        queries += 1
        raw.extend(found)
    else:
        reddit_status = {"status": "ok", "queries": queries,
                         "raw_found": len(raw)}
    kept, dropped = filter_threads(raw, keywords, now)
    sources = {
        "reddit": reddit_status,
        "x": {"status": SOURCES["x"]["status"],
              "reason": SOURCES["x"]["reason"]},
        "threads": {"status": SOURCES["threads"]["status"],
                    "reason": SOURCES["threads"]["reason"]},
    }
    return {"threads": kept, "dropped": dropped, "sources": sources}

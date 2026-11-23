"""From a business profile to the searches that look for its buyers.

Each query records where it came from (``origin``: the profile claim
ids) and why (``why``), so every result can be traced back to a fact or
a labelled AI hypothesis about the business.

Order, best first:
1. AI search phrases — how a buyer might word the need (hypotheses).
2. Offerings — what the site sells, without the brand name (a search
   for the brand finds the business itself, not its buyers).
3. Problems — the needs the AI derived from the facts.
4. Recurring terms — the site's own vocabulary.

Templates are only added in English ("best …"); for other languages
the profile's own words are used as they are. Nothing here calls the
network.
"""

from __future__ import annotations

import re

from core.audience.untrusted import clean, looks_like_injection

MAX_QUERIES = 8


def _norm(text: str) -> str:
    return " ".join(w.strip("_") for w in re.findall(r"[\w+#]+", text.lower())
                    if w.strip("_"))


def _brand_phrases(claims: list[dict]) -> list[str]:
    return [_norm(c["value"]) for c in claims
            if c["field"] == "name" and c["kind"] == "fact" and _norm(c["value"])]


def _strip_brand(text: str, phrases: list[str]) -> str:
    norm = _norm(text)
    for phrase in phrases:
        norm = (" " + norm + " ").replace(" " + phrase + " ", " ").strip()
    return norm


def plan(claims: list[dict], *, language: str = "en", country: str = "",
         max_queries: int = MAX_QUERIES) -> list[dict]:
    """[{"id", "text", "why", "origin", "language", "country"}]."""
    phrases = _brand_phrases(claims)
    brand = {w for p in phrases for w in p.split()}
    lang = (language or "en").lower()[:2]
    out: list[dict] = []
    seen: set[str] = set()

    def add(text: str, why: str, origin: list[str]) -> None:
        text = clean(text, 100).strip(" .?!")
        key = _norm(text)
        if (not key or key in seen or len(key) < 3 or looks_like_injection(text)
                or len(out) >= max_queries):
            return
        words = key.split()
        # The brand's own name, or a piece of it, finds the business
        # itself, not its buyers. A single word is too broad to search.
        if any(p and p in key for p in phrases) or (
                len(words) > 1 and set(words) <= brand) or len(words) < 2:
            return
        seen.add(key)
        out.append({"id": f"q{len(out) + 1}", "text": text, "why": why,
                    "origin": origin, "language": lang, "country": country.lower()[:2]})

    by_field: dict[str, list[dict]] = {}
    for c in claims:
        by_field.setdefault(c["field"], []).append(c)

    for c in by_field.get("search_phrase", [])[:4]:
        add(c["value"], "AI search phrase (hypothesis)", [c["id"]])
    for c in by_field.get("offering", [])[:3]:
        name = _strip_brand(c["value"], phrases)
        if len(name.split()) < 2:
            continue  # only the brand, or one generic word, was left
        add(f"best {name}" if lang == "en" else name, "what the site sells", [c["id"]])
    for c in by_field.get("problem", [])[:2]:
        add(c["value"], "a customer problem (hypothesis)", [c["id"]])
    for c in by_field.get("keyword", [])[:6]:
        add(c["value"], "a term the site uses often", [c["id"]])
    return out


def topic_terms(claims: list[dict]) -> list[str]:
    """Words describing the business's topic (for picking communities)."""
    keep = ("keyword", "offering", "segment", "search_phrase")
    return [c["value"] for c in claims if c["field"] in keep]

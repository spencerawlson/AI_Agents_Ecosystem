"""AI hypotheses about a business, grounded in its own website facts.

Rules can read a price or a product name off a page; they cannot say
who the customers are or what problem they have. A model can guess —
so its output is stored as a *hypothesis*, never as a fact, and only
when it cites the facts it rests on.

- Input: the fact/derived claims from ``website.collect`` (their ids,
  fields and values), cleaned and fenced as untrusted data. Claims whose
  text looks like an injection attempt are left out.
- Output: per field, short plain-text hypotheses, each with ``basis``
  (claim ids). A hypothesis with no valid basis, or text that fails
  ``untrusted.safe_short_text``, is dropped.
- No numbers are asked for: no market sizes, volumes or prices.

``gateway`` is injected (``core.llm.LLMGateway`` in production, a fake in
tests). Any failure returns an empty list plus the reason — the profile
is still complete with its facts.
"""

from __future__ import annotations

from core.audience.untrusted import (SYSTEM_RULE, clean, fence,
                                     looks_like_injection, safe_short_text)

#: field -> what the model is asked for (and the most kept per field).
FIELDS = {
    "segment": ("customer segments most likely to buy", 5),
    "problem": ("problems or needs these customers are trying to solve", 6),
    "motivation": ("reasons that would make them buy now", 5),
    "barrier": ("reasons that could stop them buying", 5),
    "alternative": ("kinds of alternatives they might compare it with "
                    "(categories, not company names)", 5),
    "search_phrase": ("phrases a buyer would type into a search engine or "
                      "forum when looking for this", 10),
}
_MAX_FACTS = 60


def _fact_lines(claims: list[dict]) -> tuple[list[str], set[str]]:
    lines, ids = [], set()
    for c in claims:
        if c.get("kind") not in ("fact", "derived"):
            continue
        value = clean(c.get("value"), 200)
        quotes = " ".join(e.get("quote", "") for e in c.get("evidence", []))
        if not value or looks_like_injection(value) or looks_like_injection(quotes):
            continue
        lines.append(f"{c['id']} | {c['field']} | {value}")
        ids.add(c["id"])
        if len(lines) >= _MAX_FACTS:
            break
    return lines, ids


def build_prompt(claims: list[dict], language: str = "en") -> tuple[str, set[str]]:
    lines, ids = _fact_lines(claims)
    block, _ = fence(lines)
    asks = "\n".join(f'- "{k}": {desc} (at most {n})' for k, (desc, n) in FIELDS.items())
    prompt = (
        "Below are facts read from one business's own website, one per line as "
        "`id | field | value`.\n\n"
        f"{block}\n\n"
        "Using ONLY these facts, write hypotheses for:\n"
        f"{asks}\n\n"
        "Each hypothesis is one short plain sentence or phrase (no links, no "
        "numbers you were not given, no company names) and lists the ids of the "
        f"facts it rests on. Write them in the language with code '{clean(language, 10)}'.\n"
        'Return ONLY JSON: {"segment": [{"text": "...", "basis": ["c1"]}], ...}')
    return prompt, ids


def generate(claims: list[dict], gateway, *, language: str = "en") -> tuple[list[dict], str]:
    """(hypothesis claims, note). The note says why the list is empty."""
    prompt, ids = build_prompt(claims, language)
    if not ids:
        return [], "no usable facts to reason from"
    try:
        result = gateway.complete(prompt, tier="cheap", system=SYSTEM_RULE,
                                  json_mode=True, timeout=90)
    except Exception as exc:  # noqa: BLE001 - LLMUnavailable or anything else
        return [], f"AI unavailable: {clean(exc, 160)}"
    payload = result.get("json") if isinstance(result, dict) else None
    if not isinstance(payload, dict):
        return [], "the AI returned no usable JSON"
    by_id = {c["id"]: c for c in claims if c.get("id") in ids}
    out: list[dict] = []
    for field, (_, limit) in FIELDS.items():
        items = payload.get(field)
        if not isinstance(items, list):
            continue
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            text = safe_short_text(item.get("text"))
            basis = [b for b in (item.get("basis") or []) if isinstance(b, str) and b in by_id]
            if not text or not basis or text.lower() in seen:
                continue
            seen.add(text.lower())
            out.append({
                "field": field, "value": text, "kind": "hypothesis",
                "confidence": "low", "basis": basis[:5],
                "evidence": [e for b in basis[:2] for e in by_id[b]["evidence"][:1]],
            })
            if len([c for c in out if c["field"] == field]) >= limit:
                break
    return out, "" if out else "the AI returned nothing grounded in the facts"

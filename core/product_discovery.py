"""Trend-driven product discovery: ideas -> live demand check -> briefs.

1. Ideas come from an IdeaSource: the LLM (proposes physical products
   for given niches) or a seed list the owner provides.
2. Each idea's search keyword is checked against Google Trends (12-month
   direction). Falling demand is dropped; rising ranks first.
3. Survivors become ProductBriefs (no suppliers yet — supplier search
   fills those in).

Never raises on a data outage: without Trends, ideas pass through with
trend "unknown" and a lower score, and the report says so.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from core.sourcing import ProductBrief

log = logging.getLogger("core.product_discovery")

_TREND_POINTS = {"rising": 3.0, "flat": 1.5, "unknown": 1.0, "falling": 0.0}

# Product categories that need licences/certifications or carry liability
# a solo store should not take on. Ideas matching these are dropped.
RESTRICTED_TERMS = (
    "supplement", "vitamin", "medicine", "drug", "cbd", "vape", "e-cig",
    "weapon", "knife", "taser", "pepper spray", "baby formula", "car seat",
    "infant sleep", "contact lens", "medical device", "hoverboard",
    "lithium battery pack", "cosmetic injection", "teeth whitening",
)


@dataclass
class ProductIdea:
    product_name: str
    niche: str
    keywords: list[str]
    description: str = ""
    product_type: str = ""
    market_price_usd: list[float] | None = None
    rationale: str = ""
    trend: str = "unknown"
    trend_avg: float | None = None
    score: float = 0.0
    flags: list[str] = field(default_factory=list)


class IdeaSource(Protocol):
    def ideas(self, niches: list[str], limit: int) -> list[ProductIdea]: ...


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def is_restricted(idea: ProductIdea) -> bool:
    text = f"{idea.product_name} {idea.niche} {' '.join(idea.keywords)}".lower()
    return any(term in text for term in RESTRICTED_TERMS)


class SeedIdeaSource:
    """Owner-provided product ideas (no LLM needed)."""

    def __init__(self, seeds: list[dict]) -> None:
        self.seeds = seeds

    def ideas(self, niches: list[str], limit: int) -> list[ProductIdea]:
        out = []
        for s in self.seeds[:limit]:
            name = s["product_name"]
            out.append(ProductIdea(
                product_name=name,
                niche=s.get("niche") or (niches[0] if niches else name),
                keywords=list(s.get("keywords") or [name.lower()]),
                description=s.get("description", ""),
                product_type=s.get("product_type", ""),
                market_price_usd=s.get("market_price_usd"),
            ))
        return out


_IDEA_SYSTEM = (
    "You are an e-commerce product researcher for a small Shopify store "
    "that dropships from Chinese suppliers. You pick products that are "
    "light, non-fragile, unbranded (no trademarks/IP), not regulated, "
    "solve a clear problem, and sell for $20-$60 retail. Be realistic."
)


class LLMIdeaSource:
    def __init__(self, gateway) -> None:
        self.gateway = gateway
        self.spend_usd = 0.0
        self.tokens = 0

    def ideas(self, niches: list[str], limit: int) -> list[ProductIdea]:
        focus = ", ".join(niches) if niches else "any niche with growing demand"
        res = self.gateway.complete(
            system=_IDEA_SYSTEM,
            prompt=(
                f"Propose {limit} physical products to test. Focus: {focus}.\n"
                "Return STRICT JSON: {\"ideas\": [{\"product_name\": str, "
                "\"niche\": str, \"keywords\": [3-5 buyer search phrases, "
                "the most-searched first], \"description\": str (factual, "
                "generic spec), \"product_type\": str, "
                "\"market_price_usd\": [low, high] typical retail on "
                "Amazon/Shopify, \"rationale\": str}]}"
            ),
        )
        self.spend_usd += res.get("cost_usd", 0.0)
        self.tokens += res.get("input_tokens", 0) + res.get("output_tokens", 0)
        out = []
        for raw in (res.get("json") or {}).get("ideas", [])[:limit]:
            try:
                band = raw.get("market_price_usd")
                if not (isinstance(band, list) and len(band) == 2):
                    band = None
                out.append(ProductIdea(
                    product_name=str(raw["product_name"]).strip(),
                    niche=str(raw.get("niche", "")).strip(),
                    keywords=[str(k).strip().lower()
                              for k in raw.get("keywords", []) if str(k).strip()],
                    description=str(raw.get("description", "")),
                    product_type=str(raw.get("product_type", "")),
                    market_price_usd=[float(band[0]), float(band[1])] if band else None,
                    rationale=str(raw.get("rationale", "")),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return out


def check_demand(ideas: list[ProductIdea], trends) -> None:
    """Annotate ideas with Trends direction (in place). trends may be None."""
    if trends is None:
        for idea in ideas:
            idea.flags.append("demand not verified (Google Trends unavailable)")
        return
    for i in range(0, len(ideas), 3):
        batch = ideas[i:i + 3]
        kws = [b.keywords[0] if b.keywords else b.product_name.lower()
               for b in batch]
        try:
            data = trends.interest(kws)
        except Exception as exc:  # noqa: BLE001 - outage degrades, never crashes
            log.warning("trends check failed: %s", exc)
            data = {}
        for idea, kw in zip(batch, kws):
            t = data.get(kw)
            if t:
                idea.trend = t["trend"]
                idea.trend_avg = t["avg_12mo"]
            else:
                idea.flags.append("demand not verified (no Trends data)")


def score_idea(idea: ProductIdea) -> float:
    """Trend direction dominates; price band in the dropship sweet spot helps."""
    score = _TREND_POINTS.get(idea.trend, 1.0) * 20
    if idea.trend_avg is not None:
        score += min(idea.trend_avg, 100) * 0.2
    if idea.market_price_usd:
        low, high = sorted(idea.market_price_usd)
        if 20 <= (low + high) / 2 <= 60:
            score += 15
        else:
            idea.flags.append(f"retail band ${low:.0f}-${high:.0f} outside $20-$60")
    else:
        idea.flags.append("no market price band")
    if len(idea.keywords) < 2:
        idea.flags.append("few search keywords")
    return round(score, 1)


def discover(source: IdeaSource, niches: list[str], limit: int = 8,
             trends=None, keep: int = 3) -> tuple[list[ProductBrief], list[ProductIdea]]:
    """Return (briefs for the best `keep` ideas, all evaluated ideas)."""
    ideas = [i for i in source.ideas(niches, limit) if i.product_name]
    for idea in ideas:
        if is_restricted(idea):
            idea.flags.append("restricted category — dropped")
    candidates = [i for i in ideas if not is_restricted(i)]
    check_demand(candidates, trends)
    for idea in candidates:
        idea.score = score_idea(idea)
    viable = [i for i in candidates if i.trend != "falling"]
    viable.sort(key=lambda i: i.score, reverse=True)
    briefs = [
        ProductBrief(
            id=_slug(i.product_name),
            niche=i.niche or i.product_name,
            product_name=i.product_name,
            keywords=i.keywords or [i.product_name.lower()],
            description=i.description,
            product_type=i.product_type,
            market_price_usd=i.market_price_usd,
            market_price_source="llm estimate" if i.rationale else "owner seed",
            demand_trend=i.trend,
        )
        for i in viable[:keep]
    ]
    return briefs, ideas

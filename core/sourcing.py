"""Supplier sourcing: candidates, scoring, and the owner-facing report.

Alibaba has no public buyer-side search API and scraping it breaks its
terms, so candidates enter through a SupplierSource. Today that is the
product brief itself (the owner or a sourcing data service fills in the
supplier list); a data-service source can implement the same protocol
later without touching the pipeline.

Every candidate is priced with core.pricing at the brief's target
margin, so the owner sees what each supplier means for the shelf price
before contacting anyone.
"""

from __future__ import annotations

import urllib.parse
from typing import Protocol

from pydantic import BaseModel, Field

from core.pricing import DEFAULT_TARGET_MARGIN, FeeSchedule, price_for_margin


class Supplier(BaseModel):
    name: str
    url: str = ""
    platform: str = "alibaba"
    unit_cost_usd: float = Field(ge=0)
    # Cost to ship ONE unit to the end customer (dropship) or the
    # per-unit share of freight (bulk). Shelf price includes it.
    shipping_cost_usd: float = Field(default=0.0, ge=0)
    other_cost_usd: float = Field(default=0.0, ge=0)  # packaging, branding
    moq: int = Field(default=1, ge=1)
    lead_time_days: int | None = None      # order -> delivered to customer
    rating: float | None = None            # 0-5
    years_on_platform: int | None = None
    verified: bool = False                 # Verified / Gold supplier
    trade_assurance: bool = False
    image_urls: list[str] = Field(default_factory=list)
    notes: str = ""
    # Machine ids for automated ordering (CJ product pid / variant vid).
    supplier_product_id: str = ""
    supplier_variant_id: str = ""
    shipping_method: str = ""


class ProductBrief(BaseModel):
    id: str
    niche: str
    product_name: str
    keywords: list[str] = Field(default_factory=list)
    description: str = ""
    product_type: str = ""
    vendor: str = ""
    target_margin: float = DEFAULT_TARGET_MARGIN
    # Typical competitor price band, if known: [low, high].
    market_price_usd: list[float] | None = None
    market_price_source: str = ""   # "owner", "llm estimate", ...
    demand_trend: str = ""          # Google Trends 12-month direction
    suppliers: list[Supplier] = Field(default_factory=list)


class ScoredSupplier(BaseModel):
    supplier: Supplier
    quote: dict
    score: float
    reasons: list[str]
    flags: list[str]


class SupplierSource(Protocol):
    def find(self, brief: ProductBrief) -> list[Supplier]: ...


class BriefSupplierSource:
    """Suppliers listed in the brief (filled by the owner or a data service)."""

    def find(self, brief: ProductBrief) -> list[Supplier]:
        return list(brief.suppliers)


def alibaba_search_url(query: str) -> str:
    return ("https://www.alibaba.com/trade/search?"
            + urllib.parse.urlencode({"SearchText": query}))


def score_supplier(supplier: Supplier, brief: ProductBrief,
                   fees: FeeSchedule = FeeSchedule()) -> ScoredSupplier:
    """0-100 score: price competitiveness, reliability, logistics."""
    q = price_for_margin(supplier.unit_cost_usd, supplier.shipping_cost_usd,
                         supplier.other_cost_usd,
                         target_margin=brief.target_margin, fees=fees)
    reasons: list[str] = []
    flags: list[str] = []

    # Price competitiveness (40): can we hit the margin at a market price?
    price_pts = 25.0
    if brief.market_price_usd and len(brief.market_price_usd) == 2:
        low, high = sorted(brief.market_price_usd)
        if q.price_usd <= low:
            price_pts = 40.0
            reasons.append(f"price ${q.price_usd} at/below market low ${low}")
        elif q.price_usd <= high:
            span = max(high - low, 0.01)
            price_pts = 40.0 - 25.0 * (q.price_usd - low) / span
            reasons.append(f"price ${q.price_usd} inside market ${low}-${high}")
        else:
            price_pts = 0.0
            flags.append(f"price ${q.price_usd} above market high ${high} "
                         f"at {brief.target_margin:.0%} margin")
    else:
        reasons.append("no market price band in brief (neutral price score)")

    # Reliability (40)
    rel = 0.0
    if supplier.verified:
        rel += 12
        reasons.append("verified supplier")
    if supplier.trade_assurance:
        rel += 10
        reasons.append("trade assurance")
    else:
        flags.append("no trade assurance")
    if supplier.years_on_platform is not None:
        rel += min(supplier.years_on_platform, 5) * 2
        if supplier.years_on_platform < 2:
            flags.append(f"only {supplier.years_on_platform}y on platform")
    if supplier.rating is not None:
        rel += max(0.0, supplier.rating - 3.0) * 4  # 5.0 -> 8 pts
        if supplier.rating < 4.3:
            flags.append(f"rating {supplier.rating}")

    # Logistics (20)
    log_pts = 10.0
    if supplier.lead_time_days is not None:
        if supplier.lead_time_days <= 10:
            log_pts = 14.0
        elif supplier.lead_time_days <= 20:
            log_pts = 9.0
        else:
            log_pts = 3.0
            flags.append(f"slow delivery ({supplier.lead_time_days} days)")
    if supplier.moq <= 10:
        log_pts += 6
    elif supplier.moq <= 100:
        log_pts += 3
    else:
        flags.append(f"MOQ {supplier.moq} ties up "
                     f"${supplier.moq * supplier.unit_cost_usd:,.0f} upfront")
    if not supplier.image_urls:
        flags.append("no product images supplied")

    score = round(min(100.0, price_pts + rel + log_pts), 1)
    return ScoredSupplier(supplier=supplier, quote=q.to_dict(), score=score,
                          reasons=reasons, flags=flags)


def rank_suppliers(brief: ProductBrief,
                   source: SupplierSource | None = None) -> list[ScoredSupplier]:
    source = source or BriefSupplierSource()
    scored = [score_supplier(s, brief) for s in source.find(brief)]
    return sorted(scored, key=lambda s: s.score, reverse=True)


def _md(text: object) -> str:
    """Neutralise characters that would break a Markdown table cell."""
    return str(text).replace("|", "/").replace("\n", " ")


def render_supplier_report(brief: ProductBrief, ranked: list[ScoredSupplier],
                           generated_at: str,
                           approval_ids: dict[str, str] | None = None) -> str:
    approval_ids = approval_ids or {}
    lines = [
        f"# Supplier Report — {_md(brief.product_name)}",
        "",
        f"_Generated {generated_at} · niche: {_md(brief.niche)} · "
        f"brief: {_md(brief.id)}_",
        "",
        "> **NOTHING WAS PUBLISHED.** Contact the suppliers you like, then "
        "approve exactly one in /approvals. Approving publishes the product "
        "to the Shopify store at the price shown. Reject the rest.",
        "",
        f"## Pricing rule",
        "",
        f"Price = lowest .99 price giving a **{brief.target_margin:.0%} margin** "
        "after product cost, shipping, extras and the payment fee. Ad spend "
        "is not in the price — see *Max ad cost / order* (keeps a "
        "20% net margin).",
        "",
    ]
    if brief.market_price_usd:
        lo, hi = sorted(brief.market_price_usd)
        lines += [f"Market price band: **${lo:.2f} – ${hi:.2f}**", ""]

    lines += ["## Ranked suppliers", ""]
    if not ranked:
        lines += ["No supplier candidates in the brief yet. Use the searches "
                  "below, then add candidates to the brief and re-run.", ""]
    else:
        lines += [
            "| # | Supplier | Score | Landed cost | Price | Profit | Margin "
            "| Max ad cost / order | MOQ | Delivery |",
            "|---|----------|-------|-------------|-------|--------|--------"
            "|---------------------|-----|----------|",
        ]
        for i, s in enumerate(ranked, 1):
            q, sup = s.quote, s.supplier
            lead = f"{sup.lead_time_days}d" if sup.lead_time_days else "?"
            lines.append(
                f"| {i} | {_md(sup.name)} | {s.score} | "
                f"${q['landed_cost_usd']:.2f} | ${q['price_usd']:.2f} | "
                f"${q['profit_usd']:.2f} | {q['margin']:.0%} | "
                f"${q['max_ad_cost_per_order_usd']:.2f} | {sup.moq} | {lead} |")
        lines.append("")
        for i, s in enumerate(ranked, 1):
            sup = s.supplier
            lines += [f"### {i}. {_md(sup.name)}", ""]
            if sup.url:
                lines.append(f"- Listing: `{_md(sup.url)}`")
            aid = approval_ids.get(sup.name)
            if aid:
                lines.append(f"- Approval: `{aid}`")
            for r in s.reasons:
                lines.append(f"- {_md(r)}")
            for f in s.flags:
                lines.append(f"- **Check:** {_md(f)}")
            if sup.notes:
                lines.append(f"- Notes: {_md(sup.notes)}")
            lines.append("")

    lines += ["## Alibaba searches", ""]
    queries = [brief.product_name] + [k for k in brief.keywords
                                      if k != brief.product_name]
    for q in queries[:6]:
        lines.append(f"- {_md(q)}: `{alibaba_search_url(q)}`")
    lines += [
        "",
        "## Before you approve — ask each supplier",
        "",
        "- Unit price at your expected volume, and sample cost",
        "- Dropship / per-order shipping to your main market, with tracking",
        "- Delivery time to customer, and who handles damaged/lost items",
        "- Can they ship without their branding (blind dropship)?",
        "- Product certifications needed for your market (CE, FCC, CPSIA…)",
        "- Permission to use their product photos",
        "",
    ]
    return "\n".join(lines)

"""Shopify store pipeline: trends -> product briefs -> supplier report ->
owner approval -> product live -> paused ads -> owner go-ahead -> live
ads -> ongoing optimisation.

  discover          product ideas (LLM or seed file) checked against
                    Google Trends; writes briefs to data/briefs/ and a
                    discovery report. --source chains into `source`.
  source            suppliers from the brief (owner's Alibaba quotes) +
                    CJ Dropshipping API search, priced at the brief's
                    target margin (default 60%), listing copy + SEO
                    drafted, report written, ONE approval per supplier.
                    Nothing is published.
  (owner)           contacts suppliers, approves exactly one in /approvals.
  publish-approved  create + publish the approved product. Idempotent.
  ads-draft         build PAUSED Meta/Google campaigns for a published
                    product and open a launch approval per platform.
  cycle             one full operations pass (what the worker runs each
                    tick): publish approved, launch approved ads,
                    auto-draft ads, spend guard, profit, SEO fixes, blog
                    drafts, store report.

Approvals persist to data/approvals.json and pipeline state to
data/shopify_state.json, so the dashboard, this script and the worker can
run as separate processes.

Usage:
    python ecosystem/shopify_pipeline.py check
    python ecosystem/shopify_pipeline.py discover --niche "home office" --llm --source
    python ecosystem/shopify_pipeline.py source examples/shopify_brief.example.json [--llm]
    python ecosystem/shopify_pipeline.py publish-approved
    python ecosystem/shopify_pipeline.py ads-draft <product_id> [--daily 10 --days 7]
    python ecosystem/shopify_pipeline.py cycle [--llm]
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sourcing import (ProductBrief, ScoredSupplier, rank_suppliers,
                           render_supplier_report)
from orchestrator.approvals import ApprovalGate
from orchestrator.models import ApprovalStatus

log = logging.getLogger("ecosystem.shopify_pipeline")

APPROVAL_ACTION = "approve_supplier"
SEO_TITLE_MAX = 70
SEO_DESCRIPTION_MAX = 160


def default_state_path() -> Path:
    env = os.environ.get("SHOPIFY_STATE_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "shopify_state.json"


STATE_SECTIONS = ("approvals", "briefs", "products", "campaigns", "articles",
                  "seo_fixes")


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}
    for key in STATE_SECTIONS:
        state.setdefault(key, {})
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


_load_state, _save_state = load_state, save_state


def _slug(text: str, max_len: int = 24) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").upper()[:max_len]


# -- Listing copy ------------------------------------------------------------

_LISTING_SYSTEM = (
    "You write Shopify product listings that rank in search and convert. "
    "Be specific and honest: no invented specs, certifications, reviews, "
    "discounts or medical claims. Only use facts given to you."
)


class ListingWriter:
    """Drafts title, description and SEO fields. LLM when available,
    deterministic template otherwise (never blocks the pipeline)."""

    def __init__(self, gateway=None) -> None:
        self.gateway = gateway
        self.spend_usd = 0.0
        self.tokens = 0

    def write(self, brief: ProductBrief) -> dict:
        if self.gateway is not None:
            try:
                return self._llm(brief)
            except Exception as exc:  # noqa: BLE001 - fall back, never crash
                log.warning("LLM listing failed, using template: %s", exc)
        return self._template(brief)

    def _llm(self, brief: ProductBrief) -> dict:
        res = self.gateway.complete(
            system=_LISTING_SYSTEM,
            prompt=(
                "Write a product listing. Return STRICT JSON with keys: "
                '"title" (<=70 chars), "description_html" (3-5 short <p>/<ul> '
                'blocks, benefits first), "seo_title" (<=70 chars, primary '
                'keyword first), "seo_description" (<=160 chars), "tags" '
                '(8-15 lowercase search phrases), "image_alt" (<=125 chars).\n'
                f"Product: {brief.product_name}\nNiche: {brief.niche}\n"
                f"Target keywords: {', '.join(brief.keywords)}\n"
                f"Facts: {brief.description or '(none given)'}"
            ),
        )
        self.spend_usd += res.get("cost_usd", 0.0)
        self.tokens += res.get("input_tokens", 0) + res.get("output_tokens", 0)
        return self._normalise(res["json"] or {}, brief)

    def _template(self, brief: ProductBrief) -> dict:
        name = brief.product_name
        facts = html.escape(brief.description) if brief.description else ""
        body = f"<p>{html.escape(name)} for {html.escape(brief.niche)}.</p>"
        if facts:
            body += f"<p>{facts}</p>"
        primary = brief.keywords[0] if brief.keywords else name
        return self._normalise({
            "title": name,
            "description_html": body,
            "seo_title": f"{primary.title()} | {name}",
            "seo_description": brief.description or f"Shop {name} — {brief.niche}.",
            "tags": brief.keywords,
            "image_alt": name,
        }, brief)

    @staticmethod
    def _normalise(data: dict, brief: ProductBrief) -> dict:
        def clip(value, limit):
            text = str(value or "").strip()
            return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

        tags = [str(t).strip().lower() for t in data.get("tags") or []
                if str(t).strip()]
        return {
            "title": clip(data.get("title") or brief.product_name, 255),
            "description_html": str(data.get("description_html") or ""),
            "seo_title": clip(data.get("seo_title") or brief.product_name,
                              SEO_TITLE_MAX),
            "seo_description": clip(data.get("seo_description"),
                                    SEO_DESCRIPTION_MAX),
            "tags": list(dict.fromkeys(tags))[:15],
            "image_alt": clip(data.get("image_alt") or brief.product_name, 125),
        }


# -- Step 1: source ----------------------------------------------------------

def _product_payload(brief: ProductBrief, scored: ScoredSupplier,
                     listing: dict) -> dict:
    sup, q = scored.supplier, scored.quote
    return {
        "title": listing["title"],
        "description_html": listing["description_html"],
        "price_usd": q["price_usd"],
        "cost_usd": q["landed_cost_usd"],
        "sku": f"{_slug(brief.id, 16)}-{_slug(sup.name, 12)}",
        "vendor": brief.vendor,
        "product_type": brief.product_type,
        "tags": listing["tags"],
        "seo_title": listing["seo_title"],
        "seo_description": listing["seo_description"],
        "image_urls": sup.image_urls,
        "image_alt": listing["image_alt"],
        "track_inventory": False,
    }


def _listing_section(listing: dict) -> str:
    def cell(x):
        return str(x).replace("|", "/").replace("\n", " ")

    return "\n".join([
        "## Listing draft (published as-is on approval)",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Title | {cell(listing['title'])} |",
        f"| SEO title | {cell(listing['seo_title'])} |",
        f"| SEO description | {cell(listing['seo_description'])} |",
        f"| Tags | {cell(', '.join(listing['tags']))} |",
        "",
        "Description HTML:",
        "",
        f"`{cell(listing['description_html'])}`",
        "",
    ])


def source(brief: ProductBrief, gate: ApprovalGate, reports_dir: Path,
           writer: ListingWriter | None = None,
           now: datetime | None = None,
           supplier_source=None) -> dict:
    """Rank suppliers, draft the listing, open approvals, write the report."""
    now = now or datetime.now(timezone.utc)
    writer = writer or ListingWriter()
    ranked = rank_suppliers(brief, supplier_source)
    source_errors = list(getattr(supplier_source, "errors", None) or [])
    listing = writer.write(brief)

    report_name = f"supplier_report_{_slug(brief.id, 40).lower()}_{now:%Y%m%d}.md"
    approval_ids: dict[str, str] = {}
    for rank, scored in enumerate(ranked, 1):
        q = scored.quote
        approval = gate.request(
            action=APPROVAL_ACTION,
            details={
                "brief_id": brief.id,
                "product_name": brief.product_name,
                "supplier_name": scored.supplier.name,
                "supplier_url": scored.supplier.url,
                "rank": rank,
                "score": scored.score,
                "price_usd": q["price_usd"],
                "margin": q["margin"],
                "landed_cost_usd": q["landed_cost_usd"],
                "fees_usd": q["fees_usd"],
                "max_ad_cost_per_order_usd": q["max_ad_cost_per_order_usd"],
                "niche": brief.niche,
                "keywords": brief.keywords,
                "facts": brief.description,
                "flags": scored.flags,
                "report_name": report_name,
                "product": _product_payload(brief, scored, listing),
            },
            requested_by="shopify_pipeline",
        )
        approval_ids[scored.supplier.name] = approval.id

    report = render_supplier_report(brief, ranked, now.isoformat(), approval_ids)
    if source_errors:
        report += ("\n## Supplier search problems\n\n"
                   + "\n".join(f"- {e}" for e in source_errors) + "\n")
    report += "\n" + _listing_section(listing)
    report += (f"\n## Spend\n\nTotal AI spend this mission: "
               f"**${writer.spend_usd:.4f}** ({writer.tokens} tokens)\n")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / report_name
    report_path.write_text(report, encoding="utf-8")
    return {"report_path": str(report_path), "approval_ids": approval_ids,
            "suppliers": len(ranked), "listing": listing,
            "source_errors": source_errors}


# -- Step 3: publish approved ------------------------------------------------

def publish_approved(gate: ApprovalGate, adapter,
                     state_path: Path | None = None) -> list[dict]:
    """Publish every approved, not-yet-handled supplier. One product per brief.

    Partial failures are resumable: a product created as a draft but not
    yet published is published on the next run, never created twice.
    """
    state_path = state_path or default_state_path()
    state = _load_state(state_path)
    results = []
    for appr in gate.all():
        if appr.action != APPROVAL_ACTION or appr.status != ApprovalStatus.APPROVED:
            continue
        rec = state["approvals"].get(appr.id, {})
        if rec.get("status") in ("published", "skipped"):
            continue
        d = appr.details
        brief_id = d.get("brief_id", "")
        owner = state["briefs"].get(brief_id)
        if owner and owner != appr.id:
            rec = {"status": "skipped",
                   "reason": f"brief {brief_id} already published via {owner}"}
            state["approvals"][appr.id] = rec
            _save_state(state_path, state)
            results.append({"approval_id": appr.id, **rec})
            continue
        try:
            if not rec.get("product_id"):
                product = adapter.create_product(d["product"])
                rec = {"status": "draft_created", "product_id": product["id"]}
                state["approvals"][appr.id] = rec
                state["briefs"][brief_id] = appr.id
                _save_state(state_path, state)
            live = adapter.publish_product(rec["product_id"])
            state["products"][rec["product_id"]] = {
                "brief_id": brief_id,
                "approval_id": appr.id,
                "title": d["product"]["title"],
                "url": live.get("onlineStoreUrl"),
                "image_url": (d["product"].get("image_urls") or [None])[0],
                "niche": d.get("niche", ""),
                "keywords": d.get("keywords", []),
                "facts": d.get("facts", ""),
                "price_usd": d["product"]["price_usd"],
                "landed_cost_usd": d.get("landed_cost_usd",
                                         d["product"].get("cost_usd", 0.0)),
                "fees_usd": d.get("fees_usd", 0.0),
                "max_ad_cost_per_order_usd": d.get("max_ad_cost_per_order_usd", 0.0),
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
            rec = {**rec, "status": "published",
                   "handle": live.get("handle"),
                   "url": live.get("onlineStoreUrl"),
                   "price_usd": d["product"]["price_usd"],
                   "supplier_name": d.get("supplier_name"),
                   "published_at": datetime.now(timezone.utc).isoformat()}
            rec.pop("error", None)
        except Exception as exc:  # noqa: BLE001 - record and retry next run
            rec = {**rec, "status": rec.get("status", "failed"),
                   "error": str(exc)[:500]}
            log.error("publish failed approval=%s: %s", appr.id, exc)
        state["approvals"][appr.id] = rec
        _save_state(state_path, state)
        results.append({"approval_id": appr.id, **rec})
    return results


# -- Discovery ---------------------------------------------------------------

def default_briefs_dir() -> Path:
    env = os.environ.get("BRIEFS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "briefs"


def discover_briefs(idea_source, niches: list[str], briefs_dir: Path,
                    reports_dir: Path, trends=None, limit: int = 8, keep: int = 3,
                    now: datetime | None = None) -> dict:
    """Ideas -> Trends check -> brief files + discovery report."""
    from core.product_discovery import _slug as idea_slug
    from core.product_discovery import discover

    now = now or datetime.now(timezone.utc)
    briefs, ideas = discover(idea_source, niches, limit=limit, trends=trends, keep=keep)
    briefs_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for b in briefs:
        path = briefs_dir / f"{b.id}.json"
        if path.exists():  # never clobber a brief the owner already filled in
            existing = ProductBrief.model_validate_json(path.read_text(encoding="utf-8"))
            if existing.suppliers:
                paths.append(path)
                continue
        path.write_text(b.model_dump_json(indent=2), encoding="utf-8")
        paths.append(path)

    def cell(x):
        return str(x).replace("|", "/").replace("\n", " ")

    kept = {b.id for b in briefs}
    lines = [f"# Product Discovery — {cell(', '.join(niches) or 'open search')}", "",
             f"_Generated {now.isoformat()}_", "",
             "> Briefs were written for the top ideas. Nothing was sourced or "
             "published.", "",
             "| Idea | Niche | Trend (12 mo) | Retail band | Score | Kept | Flags |",
             "|------|-------|---------------|-------------|-------|------|-------|"]
    for i in sorted(ideas, key=lambda x: x.score, reverse=True):
        band = (f"${i.market_price_usd[0]:.0f}-${i.market_price_usd[1]:.0f}"
                if i.market_price_usd else "?")
        lines.append(f"| {cell(i.product_name)} | {cell(i.niche)} | {i.trend} | {band} | "
                     f"{i.score} | {'yes' if idea_slug(i.product_name) in kept else ''} | "
                     f"{cell('; '.join(i.flags))} |")
    spend = getattr(idea_source, "spend_usd", 0.0)
    tokens = getattr(idea_source, "tokens", 0)
    lines += ["", "## Briefs", ""] + [f"- `{pth}`" for pth in paths]
    lines += ["", "## Spend", "",
              f"Total AI spend this mission: **${spend:.4f}** ({tokens} tokens)", ""]
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = reports_dir / f"product_discovery_{now:%Y%m%d}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return {"briefs": briefs, "brief_paths": [str(x) for x in paths],
            "ideas": ideas, "report_path": str(report)}


# -- CLI ---------------------------------------------------------------------

def _gateway(want: bool):
    if not want:
        return None
    from core.llm import LLMGateway
    if not LLMGateway.enabled():
        print("--llm given but no LLM key/litellm available; using templates",
              file=sys.stderr)
        return None
    return LLMGateway()


def _shopify_or_none():
    from core.shopify_adapter import ShopifyCommerceAdapter, ShopifyCredentialsError
    try:
        return ShopifyCommerceAdapter.from_env()
    except ShopifyCredentialsError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify Shopify / CJ / ads credentials")
    p_disc = sub.add_parser("discover", help="trend-checked product briefs")
    p_disc.add_argument("--niche", action="append", default=[],
                        help="focus niche (repeatable)")
    p_disc.add_argument("--seeds", type=Path,
                        help="JSON list of {product_name, keywords, ...} ideas")
    p_disc.add_argument("--limit", type=int, default=8)
    p_disc.add_argument("--keep", type=int, default=3)
    p_disc.add_argument("--no-trends", action="store_true")
    p_disc.add_argument("--source", action="store_true",
                        help="run supplier sourcing for each new brief")
    p_disc.add_argument("--llm", action="store_true")
    p_src = sub.add_parser("source", help="supplier report + approvals")
    p_src.add_argument("brief", type=Path)
    p_src.add_argument("--llm", action="store_true",
                       help="write listing copy with the LLM gateway")
    sub.add_parser("publish-approved", help="publish approved suppliers")
    p_ads = sub.add_parser("ads-draft", help="paused campaigns for a product")
    p_ads.add_argument("product_id")
    p_ads.add_argument("--daily", type=float)
    p_ads.add_argument("--days", type=int)
    p_ads.add_argument("--llm", action="store_true")
    p_cyc = sub.add_parser("cycle", help="one full store operations pass")
    p_cyc.add_argument("--llm", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    from core.ads import AdCopyWriter, configured_ads_adapters
    from core.supplier_search import CJDropshippingSource, default_supplier_source
    from dashboard.reports import reports_dir
    from ecosystem.runtime import default_approvals_path

    gate = ApprovalGate(persist_path=os.environ.get("APPROVALS_PATH")
                        or default_approvals_path())
    gateway = _gateway(getattr(args, "llm", False))

    if args.cmd == "discover":
        from core.market_data import TrendsClient
        from core.product_discovery import LLMIdeaSource, SeedIdeaSource

        if args.seeds:
            idea_source = SeedIdeaSource(json.loads(args.seeds.read_text(encoding="utf-8")))
        elif gateway is not None:
            idea_source = LLMIdeaSource(gateway)
        else:
            print("discover needs --llm (with an API key) or --seeds FILE", file=sys.stderr)
            return 2
        out = discover_briefs(idea_source, args.niche, default_briefs_dir(), reports_dir(),
                              trends=None if args.no_trends else TrendsClient(),
                              limit=args.limit, keep=args.keep)
        print(f"report: {out['report_path']}")
        for pth in out["brief_paths"]:
            print(f"brief: {pth}")
        if args.source:
            for pth in out["brief_paths"]:
                brief = ProductBrief.model_validate_json(Path(pth).read_text(encoding="utf-8"))
                res = source(brief, gate, reports_dir(), ListingWriter(gateway),
                             supplier_source=default_supplier_source())
                print(f"{brief.id}: {res['suppliers']} supplier approval(s); "
                      f"report {res['report_path']}")
        return 0

    if args.cmd == "source":
        brief = ProductBrief.model_validate_json(
            args.brief.read_text(encoding="utf-8"))
        if not CJDropshippingSource.configured():
            print("note: CJ_API_KEY not set - only suppliers listed in the brief "
                  "are used", file=sys.stderr)
        out = source(brief, gate, reports_dir(), ListingWriter(gateway),
                     supplier_source=default_supplier_source())
        print(f"report: {out['report_path']}")
        print(f"{out['suppliers']} supplier approval(s) opened - review at "
              f"/approvals, approve ONE")
        for e in out["source_errors"]:
            print(f"supplier search problem: {e}", file=sys.stderr)
        return 0

    shopify = _shopify_or_none()
    ads = configured_ads_adapters()

    if args.cmd == "check":
        if shopify is None:
            print("Shopify: NOT configured (SHOPIFY_SHOP + SHOPIFY_ACCESS_TOKEN or "
                  "SHOPIFY_CLIENT_ID/SECRET)")
        else:
            info = shopify.shop_info()
            print(f"Shopify: connected to {info['name']} ({info['myshopifyDomain']}) "
                  f"currency={info['currencyCode']} api={shopify.api_version}")
        cj = "configured" if CJDropshippingSource.configured() else "NOT configured (CJ_API_KEY)"
        print(f"CJ Dropshipping: {cj}")
        print(f"Ads: {', '.join(ads) if ads else 'none configured (META_* / GOOGLE_ADS_*)'}")
        return 0 if shopify is not None else 2

    if shopify is None:
        print("Shopify not configured: set SHOPIFY_SHOP and SHOPIFY_ACCESS_TOKEN "
              "(or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET).", file=sys.stderr)
        return 2

    if args.cmd == "publish-approved":
        results = publish_approved(gate, shopify)
        for r in results:
            print(json.dumps(r))
        if not results:
            print("nothing approved to publish")
        return 1 if any(r.get("error") for r in results) else 0

    if args.cmd == "ads-draft":
        from ecosystem.ads_pipeline import AdsPolicy, draft_campaigns

        if not ads:
            print("no ad platform configured (META_* or GOOGLE_ADS_* env vars)",
                  file=sys.stderr)
            return 2
        state_path = default_state_path()
        state = load_state(state_path)
        pid = args.product_id
        if pid not in state["products"]:
            pid = f"gid://shopify/Product/{pid}"
        results = draft_campaigns(state, pid, ads, gate, AdCopyWriter(gateway),
                                  AdsPolicy.from_env(), reports_dir(),
                                  daily_budget_usd=args.daily, duration_days=args.days)
        save_state(state_path, state)
        for r in results:
            print(json.dumps(r))
        if not results:
            print("campaigns already drafted for this product")
        return 1 if any(r["status"] == "failed" for r in results) else 0

    from ecosystem.store_ops import run_store_cycle

    summary = run_store_cycle(gate, shopify, ads, default_state_path(), reports_dir(),
                              gateway=gateway)
    print(json.dumps({k: v for k, v in summary.items() if k != "performance"},
                     indent=2, default=str))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

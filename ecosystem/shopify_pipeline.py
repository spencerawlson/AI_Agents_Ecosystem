"""Shopify product pipeline: brief -> supplier report -> owner approval
-> product live on the store.

  1. `source <brief.json>`   rank suppliers, price each at the brief's
     target margin (default 60%), draft the listing copy + SEO, write a
     report to reports/, and open ONE approval per supplier. Nothing is
     published.
  2. Owner contacts suppliers, then approves exactly one supplier in the
     dashboard (/approvals) and rejects the rest.
  3. `publish-approved`      for every approved supplier not yet handled:
     create the product on Shopify and publish it to the Online Store.
     Idempotent — safe to run on a schedule; one product per brief.

Approvals persist to data/approvals.json and publish state to
data/shopify_state.json, so the dashboard, this script and the worker can
run as separate processes.

Usage:
    python ecosystem/shopify_pipeline.py check
    python ecosystem/shopify_pipeline.py source examples/shopify_brief.example.json [--llm]
    python ecosystem/shopify_pipeline.py publish-approved
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


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"approvals": {}, "briefs": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


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
           now: datetime | None = None) -> dict:
    """Rank suppliers, draft the listing, open approvals, write the report."""
    now = now or datetime.now(timezone.utc)
    writer = writer or ListingWriter()
    ranked = rank_suppliers(brief)
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
                "flags": scored.flags,
                "report_name": report_name,
                "product": _product_payload(brief, scored, listing),
            },
            requested_by="shopify_pipeline",
        )
        approval_ids[scored.supplier.name] = approval.id

    report = render_supplier_report(brief, ranked, now.isoformat(), approval_ids)
    report += "\n" + _listing_section(listing)
    report += (f"\n## Spend\n\nTotal AI spend this mission: "
               f"**${writer.spend_usd:.4f}** ({writer.tokens} tokens)\n")
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / report_name
    report_path.write_text(report, encoding="utf-8")
    return {"report_path": str(report_path), "approval_ids": approval_ids,
            "suppliers": len(ranked), "listing": listing}


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


# -- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify Shopify credentials")
    p_src = sub.add_parser("source", help="supplier report + approvals")
    p_src.add_argument("brief", type=Path)
    p_src.add_argument("--llm", action="store_true",
                       help="write listing copy with the LLM gateway")
    sub.add_parser("publish-approved", help="publish approved suppliers")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    from dashboard.reports import reports_dir
    from ecosystem.runtime import default_approvals_path

    gate = ApprovalGate(persist_path=os.environ.get("APPROVALS_PATH")
                        or default_approvals_path())

    if args.cmd == "source":
        brief = ProductBrief.model_validate_json(
            args.brief.read_text(encoding="utf-8"))
        gateway = None
        if args.llm:
            from core.llm import LLMGateway
            if not LLMGateway.enabled():
                print("--llm given but no LLM key/litellm available; "
                      "using template copy", file=sys.stderr)
            else:
                gateway = LLMGateway()
        out = source(brief, gate, reports_dir(), ListingWriter(gateway))
        print(f"report: {out['report_path']}")
        print(f"{out['suppliers']} supplier approval(s) opened - review at "
              f"/approvals, approve ONE")
        return 0

    from core.shopify_adapter import (ShopifyCommerceAdapter,
                                      ShopifyCredentialsError)

    try:
        adapter = ShopifyCommerceAdapter.from_env()
    except ShopifyCredentialsError as exc:
        print(f"Shopify not configured: {exc}. Set SHOPIFY_SHOP and "
              "SHOPIFY_ACCESS_TOKEN (or SHOPIFY_CLIENT_ID + "
              "SHOPIFY_CLIENT_SECRET).", file=sys.stderr)
        return 2
    if args.cmd == "check":
        info = adapter.shop_info()
        print(f"connected: {info['name']} ({info['myshopifyDomain']}) "
              f"currency={info['currencyCode']} api={adapter.api_version}")
        return 0

    results = publish_approved(gate, adapter)
    for r in results:
        print(json.dumps(r))
    if not results:
        print("nothing approved to publish")
    return 1 if any(r.get("error") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Farelune growth program: the ecosystem's first REAL own-site business.

Spencer's call (2026-10-18): make the ecosystem real, own sites first —
and Farelune is the site for now (ProofFlow's program stands ready for
when that site goes live). Farelune is already a real business in the
world: live at farelune.com, real visitors, a real Meta ad flight, and
Travelpayouts affiliate partnerships (Aviasales, Kiwi.com, Klook).

What "real" means here, concretely:
  - One business record ("Farelune") in the real registry.
  - Every agent step's actual cost is posted to its ledger as ai_spend.
  - Money in and out of the real business is bookable with its source
    named in the ledger entry: Travelpayouts payouts as revenue
    (--record-revenue, owner-reported from the partner dashboard) and
    Meta ad spend as a cost (--record-cost). Nothing estimated or
    invented; an API sync can replace hand-recording later.
  - The growth run itself is the SEO engine working the live site:
    real sitemap inventory, live Trends keyword scoring (from a
    residential IP — datacenter IPs get 429s), gate-checked article
    drafts, real spend on the books.

Usage (FedServ):
    export OPENAI_API_KEY=...
    python3 ecosystem/farelune_growth.py                  # growth run
    python3 ecosystem/farelune_growth.py --record-revenue 23.10 \
        --period 2026-10                                  # Travelpayouts $
    python3 ecosystem/farelune_growth.py --record-cost 36.90 \
        --label "Meta campaign Oct 13-27 (USD equiv of $50 CAD flight)"
    python3 ecosystem/farelune_growth.py --pnl            # show the books
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.models import BusinessStatus
from core.seo_content import fetch_inventory, suggest_internal_links, \
    word_count
from ecosystem.marketing_mission import _require_llm_ready, run_step
from ecosystem.runtime import build_runtime

SITE_URL = "https://farelune.com"
BUSINESS_NAME = "Farelune"
NICHE = "flight comparison and travel planning"
SEEDS = ["cheap flights", "flights to paris", "flights to tokyo",
         "cheap flights to cancun", "best time to visit rome",
         "how to find cheap flights"]
FACTS = [
    "Farelune compares flights and shows flight times from the "
    "visitor's own city (233 countries mapped, Montreal as fallback)",
    "Farelune covers 12 destinations in English and French, each with "
    "local food, culture and landmark guides",
    "Farelune earns affiliate commissions through Travelpayouts "
    "partners including Aviasales, Kiwi.com and Klook",
    "Farelune features a rotating Country of the Week and an "
    "interactive destinations globe",
]


# ---------------------------------------------------------------------------
# Business + ledger wiring
# ---------------------------------------------------------------------------


def get_or_create_business(rt):
    """The one Farelune business. Found by name, created once."""
    for biz in rt.businesses.list():
        if biz.name == BUSINESS_NAME and \
                biz.status != BusinessStatus.TERMINATED:
            return biz
    biz = rt.businesses.create(name=BUSINESS_NAME,
                               business_type="web_product",
                               domain="farelune.com")
    rt.businesses.transition(biz.id, BusinessStatus.RESEARCHING)
    return biz


def post_step_cost(rt, business_id: str, run, label: str) -> None:
    cost = float(getattr(run, "cost_usd", 0.0) or 0.0)
    if cost <= 0:
        return
    rt.ledger.record(business_id=business_id, kind="ai_spend",
                     amount_usd=-cost,
                     description=f"farelune_growth: {label}")


def record_revenue(rt, business_id: str, amount_usd: float, period: str,
                   source: str = "Travelpayouts dashboard"):
    """Book owner-reported affiliate earnings as real revenue; the entry
    names its source so the books never pretend to be an API sync."""
    if amount_usd <= 0:
        raise ValueError("revenue amount must be positive")
    if not period:
        raise ValueError("a period (e.g. 2026-10) is required")
    return rt.ledger.record(
        business_id=business_id, kind="revenue", amount_usd=amount_usd,
        description=f"Affiliate earnings {period} — owner-reported "
                    f"({source})")


def record_external_cost(rt, business_id: str, amount_usd: float,
                         label: str):
    """Book a real-world cost (e.g. Meta ad spend) against the business."""
    if amount_usd <= 0:
        raise ValueError("cost amount must be positive")
    if not label:
        raise ValueError("a label naming the cost is required")
    return rt.ledger.record(
        business_id=business_id, kind="external_cost",
        amount_usd=-amount_usd, description=label)


def site_is_live(site_url: str = SITE_URL) -> bool:
    try:
        fetch_inventory(site_url)
        return True
    except Exception:  # noqa: BLE001
        return False


def _print_pnl(rt, biz) -> None:
    pnl = rt.ledger.pnl(biz.id)
    print(f"Farelune P&L — revenue ${pnl.revenue:.2f} · "
          f"costs ${pnl.total_costs:.2f} · net ${pnl.net_profit:.2f}",
          flush=True)


# ---------------------------------------------------------------------------
# Mission
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Farelune growth program (real own-site business)")
    parser.add_argument("--record-revenue", type=float, default=None,
                        help="book affiliate earnings (USD) and exit")
    parser.add_argument("--period", default="",
                        help="period for --record-revenue, e.g. 2026-10")
    parser.add_argument("--record-cost", type=float, default=None,
                        help="book a real-world cost (USD) and exit")
    parser.add_argument("--label", default="",
                        help="label for --record-cost (names the spend)")
    parser.add_argument("--pnl", action="store_true",
                        help="print the Farelune P&L and exit")
    parser.add_argument("--count", type=int, default=3,
                        help="articles per growth run")
    parser.add_argument("--word-target", type=int, default=1500)
    parser.add_argument("--llm", dest="llm", action="store_true",
                        default=None)
    parser.add_argument("--no-llm", dest="llm", action="store_false")
    args = parser.parse_args(argv)

    rt = build_runtime(use_llm=args.llm,
                     use_postgres=bool(os.environ.get("DATABASE_URL")),
                     approvals_persist=True)
    biz = get_or_create_business(rt)

    if args.pnl:
        _print_pnl(rt, biz)
        return 0
    if args.record_revenue is not None:
        entry = record_revenue(rt, biz.id, args.record_revenue, args.period)
        print(f"booked revenue ${entry.amount_usd:.2f}: "
              f"{entry.description}", flush=True)
        _print_pnl(rt, biz)
        return 0
    if args.record_cost is not None:
        entry = record_external_cost(rt, biz.id, args.record_cost,
                                      args.label)
        print(f"booked cost ${-entry.amount_usd:.2f}: "
              f"{entry.description}", flush=True)
        _print_pnl(rt, biz)
        return 0

    if args.llm is not False:
        _require_llm_ready()

    if not site_is_live():
        print("STOPPED: farelune.com sitemap did not crawl — check the "
              "site before running growth against it.", flush=True)
        return 2

    inventory = fetch_inventory(SITE_URL)
    print(f"inventory: {len(inventory.pages)} live pages", flush=True)
    orch = rt.orchestrator
    lines: list[str] = []

    ok, result, run = run_step(
        orch, "marketing", "seo_keyword_map",
        {"niche": NICHE, "seeds": SEEDS}, biz.id, 1.0, 4000,
        ["at least 1 keyword returned",
         "no invented numbers — gaps marked 'unavailable' explicitly"])
    post_step_cost(rt, biz.id, run, "seo_keyword_map")
    if not ok:
        print(f"STOPPED: {result}", flush=True)
        return 1
    scored = sorted(
        result["keywords"],
        key=lambda k: (k.get("avg_12mo") is None, -(k.get("avg_12mo") or 0)))
    chosen = scored[: max(1, args.count)]

    from core.seo_publish import FileExportPublisher, PublisherError
    export_dir = Path("reports") / "seo" / "farelune_com"
    produced = 0
    for entry_kw in chosen:
        keyword = entry_kw["keyword"]
        candidates = suggest_internal_links(keyword, inventory, limit=8)
        if len(candidates) < 3:
            have = {c["url"] for c in candidates}
            for page in inventory.pages:
                if len(candidates) >= 3:
                    break
                if page.url not in have:
                    candidates.append(
                        {"url": page.url, "title": page.title})
        ok, result, run = run_step(
            orch, "creative", "seo_article",
            {"keyword": keyword, "niche": NICHE, "site_url": SITE_URL,
             "facts": FACTS, "link_candidates": candidates,
             "inventory_urls": sorted(inventory.urls),
             "word_target": args.word_target},
            biz.id, 3.0, 24000,
            ["article package has title, meta_description, slug, "
             "body_markdown, faq, internal_links",
             "internal links only from the live site inventory"])
        post_step_cost(rt, biz.id, run, f"seo_article {keyword!r}")
        if not ok:
            print(f"STOPPED at {keyword!r}: {result}", flush=True)
            return 1
        pkg, quality = result["package"], result["quality"]
        status = "blocked by quality gate"
        if not quality["errors"]:
            try:
                out = FileExportPublisher(export_dir).publish(
                    pkg, SITE_URL, author="Farelune")
                status = f"exported draft: {out['markdown']}"
                produced += 1
            except PublisherError as exc:
                status = f"export failed: {exc}"
        lines.append(f"- {keyword}: {word_count(pkg['body_markdown'])} "
                     f"words — {status}")
        print(f"ok: {keyword} — {status}", flush=True)

    report_path = Path("reports") / (
        f"farelune_growth_{datetime.now(timezone.utc):%Y%m%d}.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    pnl = rt.ledger.pnl(biz.id)
    report_path.write_text(
        f"# Farelune Growth Run — {SITE_URL}\n\n"
        f"> **DRAFT ARTICLES EXPORTED** to {export_dir} — nothing went "
        f"live.\n\n"
        f"Articles produced: {produced}\n\n" + "\n".join(lines) + "\n\n"
        f"## Books (real ledger)\n\n"
        f"Revenue ${pnl.revenue:.2f} · costs ${pnl.total_costs:.2f} · "
        f"net ${pnl.net_profit:.2f}\n", encoding="utf-8")
    approval = rt.approvals.request(
        action="mission_report_approval",
        details={"mission": "farelune_growth", "business_name": biz.name,
                 "site_url": SITE_URL, "report_path": str(report_path),
                 "report_name": report_path.name,
                 "articles_produced": produced,
                 "ledger_net_usd": round(pnl.net_profit, 4)},
        amount_usd=round(pnl.total_costs, 4), business_id=biz.id,
        requested_by="farelune_growth")
    print(f"report: {report_path}", flush=True)
    print(f"approval requested: {approval.id}", flush=True)
    _print_pnl(rt, biz)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

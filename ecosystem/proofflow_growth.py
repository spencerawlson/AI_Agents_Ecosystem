"""ProofFlow growth program: the ecosystem's first REAL own-site business.

Spencer's call (2026-10-18): the ecosystem stops being a simulation —
first real channel is his own sites, starting with ProofFlow
(proofflow.me, free proof-of-delivery tool, AdSense-monetized).

What "real" means here, concretely:
  - One business record ("ProofFlow") in the real registry — never a
    duplicate per run.
  - Every agent step's actual cost is posted to that business's ledger
    as ai_spend (missions previously tracked spend only in reports;
    the ledger is the P&L the game view reads).
  - Revenue enters only as owner-reported AdSense earnings via
    --record-revenue, with the source named in the ledger entry itself.
    Nothing is estimated, modeled or invented. (An AdSense API sync can
    replace the hand-recording later; the entry format already carries
    its source.)
  - The growth run refuses to operate on a site that is not live: it
    crawls proofflow.me's sitemap first and stops with guidance when
    the new site is not what's serving (as of 2026-10-18 the domain
    still serves the old host's "unavailable" page).

Usage (FedServ):
    export OPENAI_API_KEY=...
    python3 ecosystem/proofflow_growth.py                 # growth run
    python3 ecosystem/proofflow_growth.py --record-revenue 18.42 \
        --period 2026-10                                  # book AdSense $
    python3 ecosystem/proofflow_growth.py --pnl           # show the books
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.models import BusinessStatus
from core.seo_content import fetch_inventory, suggest_internal_links, \
    word_count
from ecosystem.marketing_mission import _require_llm_ready, run_step
from ecosystem.runtime import build_runtime

SITE_URL = "https://proofflow.me"
BUSINESS_NAME = "ProofFlow"
NICHE = "proof of delivery for couriers, contractors and delivery teams"
SEEDS = ["proof of delivery", "proof of delivery template",
         "proof of delivery app", "delivery confirmation",
         "pod software", "last mile delivery tracking"]
FACTS = [
    "ProofFlow is a free proof-of-delivery web tool that runs entirely "
    "in the browser — no account and no install",
    "The free tool builds a structured delivery record: each step of the "
    "job gets a photo, and the finished record is saved on the device",
    "ProofFlow's site publishes practical guides on proof of delivery, "
    "delivery confirmation and POD templates",
]


# ---------------------------------------------------------------------------
# Business + ledger wiring (the "real" part)
# ---------------------------------------------------------------------------


def get_or_create_business(rt):
    """The one ProofFlow business. Found by name, created once."""
    for biz in rt.businesses.list():
        if biz.name == BUSINESS_NAME and \
                biz.status != BusinessStatus.TERMINATED:
            return biz
    biz = rt.businesses.create(name=BUSINESS_NAME,
                               business_type="web_product",
                               domain="proofflow.me")
    rt.businesses.transition(biz.id, BusinessStatus.RESEARCHING)
    return biz


def post_step_cost(rt, business_id: str, run, label: str) -> None:
    """Post a task run's real cost to the business ledger as ai_spend."""
    cost = float(getattr(run, "cost_usd", 0.0) or 0.0)
    if cost <= 0:
        return
    rt.ledger.record(business_id=business_id, kind="ai_spend",
                     amount_usd=-cost,
                     description=f"proofflow_growth: {label}")


def record_adsense_revenue(rt, business_id: str, amount_usd: float,
                           period: str, source: str = "AdSense dashboard"):
    """Book owner-reported AdSense earnings as real revenue.

    The entry names its source in its own description — until an AdSense
    API sync exists, the figure is exactly what the owner read off the
    AdSense dashboard for that period, and the books say so."""
    if amount_usd <= 0:
        raise ValueError("revenue amount must be positive")
    if not period:
        raise ValueError("a period (e.g. 2026-10) is required")
    return rt.ledger.record(
        business_id=business_id, kind="revenue", amount_usd=amount_usd,
        description=f"AdSense earnings {period} — owner-reported "
                    f"({source})")


def site_is_live(site_url: str = SITE_URL) -> bool:
    """The new site is live iff its sitemap crawls. No sitemap, no run —
    the engine never writes content against a site that isn't up."""
    try:
        fetch_inventory(site_url)
        return True
    except Exception:  # noqa: BLE001 - any failure means not usable yet
        return False


def _print_pnl(rt, biz) -> None:
    pnl = rt.ledger.pnl(biz.id)
    print(f"ProofFlow P&L — revenue ${pnl.revenue:.2f} · "
          f"costs ${pnl.total_costs:.2f} · net ${pnl.net_profit:.2f}",
          flush=True)


# ---------------------------------------------------------------------------
# Mission
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="ProofFlow growth program (real own-site business)")
    parser.add_argument("--record-revenue", type=float, default=None,
                        help="book AdSense earnings (USD) and exit")
    parser.add_argument("--period", default="",
                        help="period for --record-revenue, e.g. 2026-10")
    parser.add_argument("--pnl", action="store_true",
                        help="print the ProofFlow P&L and exit")
    parser.add_argument("--count", type=int, default=2,
                        help="articles per growth run")
    parser.add_argument("--word-target", type=int, default=1500)
    parser.add_argument("--llm", dest="llm", action="store_true",
                        default=None)
    parser.add_argument("--no-llm", dest="llm", action="store_false")
    args = parser.parse_args(argv)

    rt = build_runtime(use_llm=args.llm, approvals_persist=True)
    biz = get_or_create_business(rt)

    if args.pnl:
        _print_pnl(rt, biz)
        return 0
    if args.record_revenue is not None:
        entry = record_adsense_revenue(rt, biz.id, args.record_revenue,
                                       args.period)
        print(f"booked revenue ${entry.amount_usd:.2f}: "
              f"{entry.description}", flush=True)
        _print_pnl(rt, biz)
        return 0

    if args.llm is not False:
        _require_llm_ready()

    if not site_is_live():
        print("STOPPED: proofflow.me is not serving the new site yet "
              "(no crawlable sitemap). Deploy the site and move DNS "
              "first — the growth run starts the day the site is live.",
              flush=True)
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
    keywords = result["keywords"]
    scored = sorted(
        keywords,
        key=lambda k: (k.get("avg_12mo") is None, -(k.get("avg_12mo") or 0)))
    chosen = scored[: max(1, args.count)]

    from core.seo_publish import FileExportPublisher, PublisherError
    export_dir = Path("reports") / "seo" / "proofflow_me"
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
                    pkg, SITE_URL, author="ProofFlow")
                status = f"exported draft: {out['markdown']}"
                produced += 1
            except PublisherError as exc:
                status = f"export failed: {exc}"
        lines.append(f"- {keyword}: {word_count(pkg['body_markdown'])} "
                     f"words — {status}")
        print(f"ok: {keyword} — {status}", flush=True)

    report_path = Path("reports") / (
        f"proofflow_growth_{datetime.now(timezone.utc):%Y%m%d}.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    pnl = rt.ledger.pnl(biz.id)
    report_path.write_text(
        f"# ProofFlow Growth Run — {SITE_URL}\n\n"
        f"> **DRAFT ARTICLES EXPORTED** to {export_dir} — nothing went "
        f"live.\n\n"
        f"Articles produced: {produced}\n\n" + "\n".join(lines) + "\n\n"
        f"## Books (real ledger)\n\n"
        f"Revenue ${pnl.revenue:.2f} · costs ${pnl.total_costs:.2f} · "
        f"net ${pnl.net_profit:.2f}\n", encoding="utf-8")
    approval = rt.approvals.request(
        action="mission_report_approval",
        details={"mission": "proofflow_growth", "business_name": biz.name,
                 "site_url": SITE_URL, "report_path": str(report_path),
                 "report_name": report_path.name,
                 "articles_produced": produced,
                 "ledger_net_usd": round(pnl.net_profit, 4)},
        amount_usd=round(pnl.total_costs, 4), business_id=biz.id,
        requested_by="proofflow_growth")
    print(f"report: {report_path}", flush=True)
    print(f"approval requested: {approval.id}", flush=True)
    _print_pnl(rt, biz)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

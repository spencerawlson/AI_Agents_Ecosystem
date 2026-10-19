"""Notion templates business: the ecosystem's discovery pick, made real.

The worker's discovery loop found "notion templates for freelancers"
and spent real research money on it. This program is its books: one
business (the same name, so it lands on the business already in the
registry), with owner-reported sales revenue and real-world costs.

The product itself lives in products/notion-templates/ (the template
pack spec, setup guide and listing copy). Sales happen on Gumroad
and/or Etsy — the owner's accounts, the owner's payouts. Revenue is
booked by hand from the channel dashboard, source named in every
entry, until an API sync exists.

    python ecosystem/notion_templates_growth.py --pnl
    python ecosystem/notion_templates_growth.py --record-revenue 29 --period 2026-10 --source Gumroad
    python ecosystem/notion_templates_growth.py --record-cost 0.20 --label "Etsy listing fee"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ecosystem.runtime import build_runtime

BUSINESS_NAME = "notion templates for freelancers"
BUSINESS_TYPE = "digital_product"


def get_or_create_business(rt):
    for biz in rt.businesses.list():
        if biz.name == BUSINESS_NAME:
            return biz
    return rt.businesses.create(BUSINESS_NAME, BUSINESS_TYPE)


def record_revenue(rt, business_id: str, amount_usd: float, period: str,
                   source: str = "Gumroad"):
    """Book owner-reported template sales as real revenue; the entry
    names its source so the books never pretend to be an API sync."""
    if amount_usd <= 0:
        raise ValueError("revenue amount must be positive")
    if not period:
        raise ValueError("a period (e.g. 2026-10) is required")
    if not source:
        raise ValueError("a source (Gumroad, Etsy, ...) is required")
    return rt.ledger.record(
        business_id=business_id, kind="revenue", amount_usd=amount_usd,
        description=f"Template sales {period} — owner-reported ({source})")


def record_external_cost(rt, business_id: str, amount_usd: float,
                         label: str):
    """Book a real-world cost (listing fees, ads) against the business."""
    if amount_usd <= 0:
        raise ValueError("cost amount must be positive")
    if not label:
        raise ValueError("a label naming the cost is required")
    return rt.ledger.record(
        business_id=business_id, kind="external_cost",
        amount_usd=-amount_usd, description=label)


def _print_pnl(rt, biz) -> None:
    pnl = rt.ledger.pnl(biz.id)
    print(f"{BUSINESS_NAME} P&L — revenue ${pnl.revenue:.2f} · "
          f"costs ${pnl.total_costs:.2f} · net ${pnl.net_profit:.2f}",
          flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Notion templates for freelancers — the books")
    parser.add_argument("--record-revenue", type=float, default=None,
                        metavar="USD", help="book template sales revenue")
    parser.add_argument("--period", default="",
                        help="period the revenue covers, e.g. 2026-10")
    parser.add_argument("--source", default="Gumroad",
                        help="sales channel the revenue came from")
    parser.add_argument("--record-cost", type=float, default=None,
                        metavar="USD", help="book a real-world cost")
    parser.add_argument("--label", default="",
                        help="what the cost was (required with --record-cost)")
    parser.add_argument("--pnl", action="store_true",
                        help="print the P&L and exit")
    args = parser.parse_args(argv)

    rt = build_runtime(
        use_postgres=bool(os.environ.get("DATABASE_URL")),
        approvals_persist=True)
    biz = get_or_create_business(rt)

    if args.pnl:
        _print_pnl(rt, biz)
        return 0
    if args.record_revenue is not None:
        try:
            entry = record_revenue(rt, biz.id, args.record_revenue,
                                   args.period, args.source)
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        print(f"booked revenue ${entry.amount_usd:.2f}: "
              f"{entry.description}", flush=True)
        _print_pnl(rt, biz)
        return 0
    if args.record_cost is not None:
        try:
            entry = record_external_cost(rt, biz.id, args.record_cost,
                                          args.label)
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        print(f"booked cost ${-entry.amount_usd:.2f}: "
              f"{entry.description}", flush=True)
        _print_pnl(rt, biz)
        return 0

    _print_pnl(rt, biz)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Ads pipeline: draft paused campaigns -> owner approval -> live.

draft_campaigns()  builds the full campaign (copy, targeting, budget) on
                   each configured ad platform in PAUSED state and opens
                   one `launch_ad_campaign` approval per platform, whose
                   amount is the total budget (daily x days). Nothing spends.
apply_approvals()  activates approved launches, applies approved budget
                   increases; rejected launches stay paused forever.

The spend guard that pauses losing campaigns lives in
ecosystem/store_ops.py and never needs approval (pausing stops spend).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.ads import AdCopyWriter, AdsAdapter, CampaignPlan
from orchestrator.approvals import ApprovalGate
from orchestrator.models import ApprovalStatus

log = logging.getLogger("ecosystem.ads_pipeline")

LAUNCH_ACTION = "launch_ad_campaign"
BUDGET_ACTION = "ad_budget_increase"


@dataclass
class AdsPolicy:
    default_daily_usd: float = 10.0
    default_days: int = 7
    countries: list[str] = field(default_factory=lambda: ["US"])
    auto_draft: bool = True
    # Spend guard (store_ops): pause when ...
    no_sale_kill_multiple: float = 2.0       # spend >= 2x CPA ceiling, 0 sales
    unprofitable_cpa_multiple: float = 1.25  # CPA > 1.25x ceiling
    min_purchases_for_judgement: int = 3
    # ... and propose scaling when CPA <= 70% of ceiling.
    scale_cpa_ratio: float = 0.7
    scale_factor: float = 1.5

    @classmethod
    def from_env(cls) -> "AdsPolicy":
        p = cls()
        e = os.environ
        if e.get("ADS_DAILY_BUDGET_USD"):
            p.default_daily_usd = float(e["ADS_DAILY_BUDGET_USD"])
        if e.get("ADS_DURATION_DAYS"):
            p.default_days = int(e["ADS_DURATION_DAYS"])
        if e.get("ADS_COUNTRIES"):
            p.countries = [c.strip().upper() for c in e["ADS_COUNTRIES"].split(",")
                           if c.strip()]
        if e.get("ADS_AUTO_DRAFT", "").lower() in ("0", "false", "no"):
            p.auto_draft = False
        return p


def campaign_key(product_id: str, platform: str) -> str:
    return f"{product_id}::{platform}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def draft_campaigns(state: dict, product_id: str, adapters: dict[str, AdsAdapter],
                    gate: ApprovalGate, writer: AdCopyWriter | None = None,
                    policy: AdsPolicy | None = None,
                    reports_dir: Path | None = None,
                    daily_budget_usd: float | None = None,
                    duration_days: int | None = None,
                    now: datetime | None = None) -> list[dict]:
    """Create paused campaigns for one published product. Mutates state."""
    policy = policy or AdsPolicy()
    writer = writer or AdCopyWriter()
    now = now or datetime.now(timezone.utc)
    product = state["products"].get(product_id)
    if product is None:
        raise ValueError(f"product {product_id} is not a published pipeline product")
    if not product.get("url"):
        raise ValueError(f"product {product_id} has no storefront URL yet")

    todo = {p: a for p, a in adapters.items()
            if campaign_key(product_id, p) not in state["campaigns"]}
    if not todo:
        return []
    copy = writer.write(product["title"], product.get("niche", ""),
                        product.get("keywords", []), product.get("facts", ""))
    report_name = f"ads_report_{_slug(product['brief_id'])}_{now:%Y%m%d}.md"
    out = []
    for platform, adapter in todo.items():
        if platform == "meta" and not product.get("image_url"):
            out.append({"platform": platform, "status": "skipped",
                        "reason": "Meta needs a product image"})
            continue
        plan = CampaignPlan(
            brief_id=product["brief_id"], product_title=product["title"],
            product_url=product["url"], image_url=product.get("image_url") or "",
            daily_budget_usd=daily_budget_usd or policy.default_daily_usd,
            duration_days=duration_days or policy.default_days,
            countries=list(policy.countries), copy=copy,
            max_cpa_usd=product.get("max_ad_cost_per_order_usd", 0.0),
        )
        key = campaign_key(product_id, platform)
        try:
            refs = adapter.create_paused(plan)
        except Exception as exc:  # noqa: BLE001
            log.error("ad draft failed %s: %s", key, exc)
            out.append({"platform": platform, "status": "failed", "error": str(exc)[:500]})
            continue
        appr = gate.request(
            action=LAUNCH_ACTION,
            amount_usd=plan.total_budget_usd,
            requested_by="ads_pipeline",
            details={
                "campaign_key": key, "platform": platform,
                "product_title": product["title"], "product_url": product["url"],
                "daily_budget_usd": plan.daily_budget_usd,
                "duration_days": plan.duration_days,
                "total_budget_usd": plan.total_budget_usd,
                "countries": plan.countries,
                "max_cpa_usd": plan.max_cpa_usd,
                "price_usd": product["price_usd"],
                "headlines": copy.headlines[:5],
                "primary_text": copy.primary_texts[0],
                "report_name": report_name,
            },
        )
        state["campaigns"][key] = {
            "status": "paused_awaiting_approval", "platform": platform,
            "product_id": product_id, "refs": refs, "plan": plan.to_dict(),
            "approval_id": appr.id, "approved_total_usd": 0.0,
            "created_at": now.isoformat(),
        }
        out.append({"platform": platform, "status": "drafted",
                    "approval_id": appr.id, "refs": refs})

    if reports_dir is not None and any(r["status"] == "drafted" for r in out):
        _write_ads_report(reports_dir / report_name, product, copy, out, policy,
                          daily_budget_usd or policy.default_daily_usd,
                          duration_days or policy.default_days, writer, now)
    return out


def _write_ads_report(path: Path, product: dict, copy, results: list[dict],
                      policy: AdsPolicy, daily: float, days: int,
                      writer: AdCopyWriter, now: datetime) -> None:
    def cell(x):
        return str(x).replace("|", "/").replace("\n", " ")

    ceiling = product.get("max_ad_cost_per_order_usd", 0.0)
    lines = [
        f"# Ad Campaigns — {cell(product['title'])}",
        "",
        f"_Generated {now.isoformat()} · product price ${product['price_usd']:.2f}_",
        "",
        "> **NOTHING IS SPENDING.** Campaigns were created PAUSED. Approve "
        "each platform in /approvals to go live; reject to leave it paused.",
        "",
        "## Budget",
        "",
        f"- ${daily:.2f}/day for {days} days = **${daily * days:.2f} max** per platform",
        f"- Target markets: {', '.join(policy.countries)}",
        f"- Cost-per-sale ceiling: **${ceiling:.2f}** (keeps a 20% net margin)",
        f"- Auto-pause: no sale after ${policy.no_sale_kill_multiple * max(ceiling, 1):.2f} "
        f"spent, or cost per sale above ${policy.unprofitable_cpa_multiple * ceiling:.2f} "
        f"after {policy.min_purchases_for_judgement} sales, or budget used up.",
        "",
        "## Platforms",
        "",
        "| Platform | Status | Detail |",
        "|----------|--------|--------|",
    ]
    for r in results:
        detail = r.get("approval_id") or r.get("reason") or r.get("error") or ""
        lines.append(f"| {r['platform']} | {r['status']} | `{cell(detail)}` |")
    lines += ["", "## Ad copy", "", "Headlines (Google ≤30 chars / Meta headline):", ""]
    lines += [f"- {cell(h)}" for h in copy.headlines]
    lines += ["", "Descriptions:", ""] + [f"- {cell(d)}" for d in copy.descriptions]
    lines += ["", "Primary text (Meta):", ""] + [f"- {cell(t)}" for t in copy.primary_texts]
    lines += ["", "Keywords (Google, phrase match):", "",
              ", ".join(cell(k) for k in copy.keywords), ""]
    if writer.warnings:
        lines += ["## Warnings", ""] + [f"- {cell(w)}" for w in writer.warnings] + [""]
    lines += ["## Spend", "", f"Total AI spend this mission: **${writer.spend_usd:.4f}** "
              f"({writer.tokens} tokens)", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def apply_approvals(state: dict, adapters: dict[str, AdsAdapter],
                    gate: ApprovalGate, now: datetime | None = None) -> list[dict]:
    """Act on decided launch / budget approvals. Idempotent. Mutates state."""
    now = now or datetime.now(timezone.utc)
    results = []
    for appr in gate.all():
        if appr.action not in (LAUNCH_ACTION, BUDGET_ACTION):
            continue
        if appr.status == ApprovalStatus.PENDING:
            continue
        key = appr.details.get("campaign_key", "")
        camp = state["campaigns"].get(key)
        if camp is None:
            continue
        adapter = adapters.get(camp["platform"])
        approved = appr.status == ApprovalStatus.APPROVED
        try:
            if appr.action == LAUNCH_ACTION:
                if camp.get("approval_id") != appr.id or camp["status"] != "paused_awaiting_approval":
                    continue
                if not approved:
                    camp["status"] = "rejected"
                    results.append({"campaign": key, "status": "rejected"})
                    continue
                if adapter is None:
                    raise RuntimeError(f"{camp['platform']} ads not configured")
                adapter.activate(camp["refs"])
                camp.update(status="active", launched_at=now.isoformat(),
                            approved_total_usd=float(appr.amount_usd or 0.0))
                camp.pop("error", None)
                results.append({"campaign": key, "status": "active"})
            else:
                if camp.get("pending_budget_approval") != appr.id:
                    continue
                new_daily = float(appr.details["new_daily_budget_usd"])
                if approved:
                    if adapter is None:
                        raise RuntimeError(f"{camp['platform']} ads not configured")
                    adapter.set_daily_budget(camp["refs"], new_daily)
                    camp["plan"]["daily_budget_usd"] = new_daily
                    camp["approved_total_usd"] = round(
                        camp.get("approved_total_usd", 0.0) + float(appr.amount_usd or 0), 2)
                camp.pop("pending_budget_approval", None)
                camp.pop("error", None)
                results.append({"campaign": key,
                                "status": "budget_increased" if approved else "budget_kept",
                                "daily_budget_usd": camp["plan"]["daily_budget_usd"]})
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            camp["error"] = str(exc)[:500]
            log.error("ads approval %s failed: %s", appr.id, exc)
            results.append({"campaign": key, "status": "error", "error": camp["error"]})
    return results

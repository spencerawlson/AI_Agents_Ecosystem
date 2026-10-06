"""Store operations loop: keeps the Shopify store profitable.

Every cycle (worker tick or `shopify_pipeline.py optimize`):

  1. Spend guard      pause any live campaign that used its approved
                      budget, spent 2x the cost-per-sale ceiling with no
                      sale, or sells above the ceiling. Pausing never
                      needs approval. Campaigns beating the ceiling get a
                      budget-increase *proposal* (approval required).
  2. Profit           units, revenue, gross profit, ad spend and net
                      profit per product from Shopify orders + ad data.
  3. SEO              audit every pipeline product; auto-fix only the SEO
                      title/meta description (low-risk, search-only
                      fields). Product title, copy and price are never
                      changed automatically.
  4. Content          one SEO buyer's-guide article per product, created
                      HIDDEN on the store blog (LLM required) — owner
                      makes it visible in Shopify admin.
  5. Report           reports/store_report_<date>.md

run_store_cycle() also publishes approved suppliers, fulfils paid orders
through CJ (ecosystem/fulfillment.py), applies ad approvals and
auto-drafts paused campaigns for newly published products.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from core.ads import AdCopyWriter, AdsAdapter
from ecosystem.ads_pipeline import (BUDGET_ACTION, AdsPolicy, apply_approvals,
                                    draft_campaigns)
from orchestrator.approvals import ApprovalGate

log = logging.getLogger("ecosystem.store_ops")

SEO_TITLE_MAX = 70
SEO_DESC_MIN, SEO_DESC_MAX = 50, 160
MIN_DESCRIPTION_CHARS = 300


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0]
    return (cut or text[:limit - 1]) + "…"


def _text(html_str: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", html_str or "")).split())


# -- 1. Spend guard -------------------------------------------------------------

def guard_campaigns(state: dict, adapters: dict[str, AdsAdapter],
                    gate: ApprovalGate, policy: AdsPolicy,
                    now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    actions = []
    for key, camp in state["campaigns"].items():
        if camp.get("status") != "active":
            continue
        adapter = adapters.get(camp["platform"])
        if adapter is None:
            continue
        try:
            ins = adapter.insights(camp["refs"])
        except Exception as exc:  # noqa: BLE001
            camp["error"] = f"insights: {exc}"[:500]
            continue
        camp["insights"] = {**asdict(ins), "cpa_usd": ins.cpa_usd, "roas": ins.roas,
                            "checked_at": now.isoformat()}
        ceiling = float(camp["plan"].get("max_cpa_usd") or 0.0)
        approved_total = float(camp.get("approved_total_usd") or 0.0)
        reason = None
        if approved_total and ins.spend_usd >= approved_total:
            reason = f"approved budget ${approved_total:.2f} spent"
        elif ins.purchases == 0 and ins.spend_usd >= policy.no_sale_kill_multiple * max(ceiling, 1.0):
            reason = f"no sales after ${ins.spend_usd:.2f}"
        elif (ins.purchases >= policy.min_purchases_for_judgement and ins.cpa_usd is not None
              and ins.cpa_usd > policy.unprofitable_cpa_multiple * ceiling):
            reason = (f"cost per sale ${ins.cpa_usd:.2f} above ceiling "
                      f"${ceiling:.2f}")
        if reason:
            try:
                adapter.pause(camp["refs"])
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                camp["error"] = f"pause failed: {exc}"[:500]
                actions.append({"campaign": key, "action": "pause_failed", "reason": reason})
                continue
            camp.update(status="paused_by_guard", pause_reason=reason,
                        paused_at=now.isoformat())
            actions.append({"campaign": key, "action": "paused", "reason": reason})
            continue
        if (ins.purchases >= policy.min_purchases_for_judgement
                and ins.cpa_usd is not None and ceiling > 0
                and ins.cpa_usd <= policy.scale_cpa_ratio * ceiling
                and not camp.get("pending_budget_approval")):
            daily = float(camp["plan"]["daily_budget_usd"])
            new_daily = round(daily * policy.scale_factor, 2)
            added = round((new_daily - daily) * int(camp["plan"]["duration_days"]), 2)
            appr = gate.request(
                action=BUDGET_ACTION, amount_usd=added, requested_by="store_ops",
                details={"campaign_key": key, "platform": camp["platform"],
                         "product_title": camp["plan"]["product_title"],
                         "current_daily_budget_usd": daily,
                         "new_daily_budget_usd": new_daily,
                         "cpa_usd": round(ins.cpa_usd, 2), "max_cpa_usd": ceiling,
                         "roas": round(ins.roas or 0, 2), "purchases": ins.purchases,
                         "spend_usd": round(ins.spend_usd, 2)})
            camp["pending_budget_approval"] = appr.id
            actions.append({"campaign": key, "action": "scale_proposed",
                            "approval_id": appr.id, "new_daily_budget_usd": new_daily})
    return actions


# -- 2. Profit --------------------------------------------------------------------

def product_performance(state: dict, shopify) -> dict[str, dict]:
    products = state["products"]
    if not products:
        return {}
    since = min(p["published_at"] for p in products.values())
    units = {pid: 0 for pid in products}
    for order in shopify.list_orders_since(since[:19] + "Z"):
        if (order.get("displayFinancialStatus") or "").upper() in ("REFUNDED", "VOIDED"):
            continue
        for li in (order.get("lineItems") or {}).get("nodes", []):
            pid = (li.get("product") or {}).get("id")
            if pid in units:
                units[pid] += int(li.get("quantity") or 0)
    ad_spend = {pid: 0.0 for pid in products}
    for camp in state["campaigns"].values():
        if camp.get("product_id") in ad_spend:
            ad_spend[camp["product_id"]] += float((camp.get("insights") or {}).get("spend_usd", 0))
    out = {}
    for pid, p in products.items():
        unit_profit = p["price_usd"] - p["landed_cost_usd"] - p.get("fees_usd", 0.0)
        n = units[pid]
        gross = n * unit_profit
        out[pid] = {"title": p["title"], "units": n,
                    "revenue_usd": round(n * p["price_usd"], 2),
                    "gross_profit_usd": round(gross, 2),
                    "ad_spend_usd": round(ad_spend[pid], 2),
                    "net_profit_usd": round(gross - ad_spend[pid], 2)}
    return out


# -- 3. SEO -------------------------------------------------------------------------

def seo_audit(detail: dict) -> list[str]:
    issues = []
    seo = detail.get("seo") or {}
    title, desc = (seo.get("title") or "").strip(), (seo.get("description") or "").strip()
    if not title:
        issues.append("seo_title_missing")
    elif len(title) > SEO_TITLE_MAX:
        issues.append("seo_title_too_long")
    if not desc:
        issues.append("seo_description_missing")
    elif not SEO_DESC_MIN <= len(desc) <= SEO_DESC_MAX:
        issues.append("seo_description_length")
    if len(_text(detail.get("descriptionHtml", ""))) < MIN_DESCRIPTION_CHARS:
        issues.append("thin_description")
    media = (detail.get("media") or {}).get("nodes", [])
    if not media:
        issues.append("no_images")
    elif any(not (m.get("alt") or "").strip() for m in media):
        issues.append("image_alt_missing")
    if len(detail.get("tags") or []) < 5:
        issues.append("few_tags")
    return issues


def seo_fix(detail: dict, record: dict, issues: list[str]) -> dict | None:
    """New (seo_title, seo_description) for the auto-fixable issues, or None."""
    seo = detail.get("seo") or {}
    title, desc = seo.get("title") or "", seo.get("description") or ""
    changed = False
    if {"seo_title_missing", "seo_title_too_long"} & set(issues):
        kw = (record.get("keywords") or [""])[0].strip()
        base = detail.get("title") or record.get("title", "")
        cand = f"{kw.title()} | {base}" if kw and kw.lower() not in base.lower() else base
        title = _clip(cand, SEO_TITLE_MAX)
        changed = True
    if {"seo_description_missing", "seo_description_length"} & set(issues):
        body = _text(detail.get("descriptionHtml", ""))
        if len(body) < SEO_DESC_MIN:
            body = (f"{detail.get('title', '')} for {record.get('niche', '')}. "
                    f"{body} Tracked shipping, secure checkout.")
        desc = _clip(body, SEO_DESC_MAX)
        changed = True
    return {"title": title, "description": desc} if changed else None


def run_seo(state: dict, shopify, now: datetime) -> list[dict]:
    out = []
    for pid, record in state["products"].items():
        try:
            detail = shopify.get_product(pid)
        except Exception as exc:  # noqa: BLE001
            out.append({"product_id": pid, "error": str(exc)[:300]})
            continue
        if not detail:
            continue
        issues = seo_audit(detail)
        fix = seo_fix(detail, record, issues)
        entry = {"product_id": pid, "title": record["title"], "issues": issues}
        if fix:
            try:
                shopify.update_seo(pid, fix["title"], fix["description"])
                entry["fixed"] = fix
                state["seo_fixes"].setdefault(pid, []).append(
                    {"at": now.isoformat(), **fix})
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)[:300]
        out.append(entry)
    return out


# -- 4. Content -----------------------------------------------------------------

_ARTICLE_SYSTEM = (
    "You write genuinely useful buyer's guides for a small online store's "
    "blog. Accurate, specific, no health/medical claims, no invented "
    "statistics or reviews. Mention the store's product once, naturally."
)


def draft_articles(state: dict, shopify, gateway, now: datetime,
                   limit: int = 2) -> tuple[list[dict], float]:
    """Hidden SEO articles for products without one. Returns (results, spend)."""
    if gateway is None:
        return [], 0.0
    out, spend = [], 0.0
    for pid, record in state["products"].items():
        if pid in state["articles"] or len(out) >= limit:
            continue
        kw = (record.get("keywords") or [record["title"]])[0]
        try:
            res = gateway.complete(system=_ARTICLE_SYSTEM, prompt=(
                "Return STRICT JSON {\"title\": str (<=70 chars, contains the "
                "keyword), \"body_html\": str (700-1000 words, <h2>/<p>/<ul>), "
                "\"summary\": str (<=160 chars), \"tags\": [3-6 strings]}.\n"
                f"Target keyword: {kw}\nAudience: {record.get('niche', '')}\n"
                f"Product: {record['title']} — {record.get('url', '')}\n"
                f"Product facts: {record.get('facts') or '(none)'}"))
            spend += res.get("cost_usd", 0.0)
            data = res.get("json") or {}
            if not data.get("title") or not data.get("body_html"):
                raise ValueError("article JSON missing title/body")
            art = shopify.create_article(_clip(data["title"], 255), data["body_html"],
                                         [str(t) for t in data.get("tags") or []][:6],
                                         summary=_clip(data.get("summary", ""), 160),
                                         published=False)
            state["articles"][pid] = {"article_id": art["id"], "title": art["title"],
                                      "keyword": kw, "created_at": now.isoformat(),
                                      "published": False}
            out.append({"product_id": pid, "article_id": art["id"], "title": art["title"]})
        except Exception as exc:  # noqa: BLE001
            out.append({"product_id": pid, "error": str(exc)[:300]})
    return out, spend


# -- 5. Report ------------------------------------------------------------------

def write_store_report(path: Path, now: datetime, perf: dict, state: dict,
                       guard: list[dict], seo: list[dict], articles: list[dict],
                       other: list[str], spend_usd: float) -> None:
    from ecosystem.fulfillment import fulfilment_overview

    def cell(x):
        return str(x).replace("|", "/").replace("\n", " ")

    lines = [f"# Store Report — {now:%Y-%m-%d}", "",
             f"_Generated {now.isoformat()}_", ""]
    fo = fulfilment_overview(state)
    lines += ["## Orders", ""]
    if fo["counts"]:
        lines.append(" · ".join(f"{k}: {v}" for k, v in sorted(fo["counts"].items())))
        if fo["attention"]:
            lines += ["", "**Needs you:**", ""]
            lines += [f"- {cell(a['order'])} ({a['status']}): {cell(a['why'])}"
                      for a in fo["attention"]]
    else:
        lines.append("No orders processed yet.")
    lines += ["", "## Profit by product", ""]
    if perf:
        lines += ["| Product | Units | Revenue | Gross profit | Ad spend | Net profit |",
                  "|---------|-------|---------|--------------|----------|------------|"]
        for p in perf.values():
            lines.append(f"| {cell(p['title'])} | {p['units']} | ${p['revenue_usd']:.2f} | "
                         f"${p['gross_profit_usd']:.2f} | ${p['ad_spend_usd']:.2f} | "
                         f"**${p['net_profit_usd']:.2f}** |")
        tot = sum(p["net_profit_usd"] for p in perf.values())
        lines += ["", f"Total net profit (before refunds/chargebacks): **${tot:.2f}**"]
    else:
        lines.append("No published pipeline products yet.")
    lines += ["", "## Campaigns", ""]
    if state["campaigns"]:
        lines += ["| Campaign | Status | Spend | Sales | Cost/sale | Ceiling | ROAS |",
                  "|----------|--------|-------|-------|-----------|---------|------|"]
        for key, c in state["campaigns"].items():
            ins = c.get("insights") or {}
            cpa = ins.get("cpa_usd")
            roas = ins.get("roas")
            lines.append(
                f"| {cell(c['plan']['product_title'][:40])} ({c['platform']}) | "
                f"{cell(c['status'])} | ${ins.get('spend_usd', 0):.2f} | "
                f"{ins.get('purchases', 0):g} | "
                f"{'$%.2f' % cpa if cpa is not None else '—'} | "
                f"${float(c['plan'].get('max_cpa_usd') or 0):.2f} | "
                f"{'%.2f' % roas if roas is not None else '—'} |")
    else:
        lines.append("No campaigns.")
    if guard or other:
        lines += ["", "## Actions this cycle", ""]
        for g in guard:
            lines.append(f"- {cell(g['campaign'])}: **{g['action']}** "
                         f"{cell(g.get('reason') or g.get('approval_id') or '')}")
        lines += [f"- {cell(o)}" for o in other]
    lines += ["", "## SEO", ""]
    for s in seo:
        status = ("fixed SEO title/description" if s.get("fixed")
                  else "error: " + s["error"] if s.get("error") else "ok")
        remaining = [i for i in s.get("issues", [])
                     if not (s.get("fixed") and i.startswith("seo_"))]
        lines.append(f"- {cell(s.get('title', s['product_id']))}: {cell(status)}"
                     + (f"; needs owner: {', '.join(remaining)}" if remaining else ""))
    if not seo:
        lines.append("Nothing to audit.")
    if articles:
        lines += ["", "## Blog drafts (hidden — publish in Shopify admin)", ""]
        lines += [f"- {cell(a.get('title') or a.get('error'))}" for a in articles]
    lines += ["", "## Spend", "", f"Total AI spend this mission: **${spend_usd:.4f}**", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# -- Cycle ----------------------------------------------------------------------

def run_store_cycle(gate: ApprovalGate, shopify, ads_adapters: dict[str, AdsAdapter],
                    state_path: Path, reports_dir: Path | None = None,
                    gateway=None, policy: AdsPolicy | None = None,
                    optimize: bool = True, now: datetime | None = None,
                    cj_orders=None, fulfilment_policy=None) -> dict:
    """One full operations pass. Never raises on a single-step failure."""
    from ecosystem.shopify_pipeline import load_state, publish_approved, save_state

    now = now or datetime.now(timezone.utc)
    policy = policy or AdsPolicy.from_env()
    summary: dict = {"published": [], "fulfilment": [], "ads": [], "drafted": [],
                     "guard": [], "seo": [], "articles": [], "errors": []}
    if shopify is not None:
        try:
            summary["published"] = publish_approved(gate, shopify, state_path)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"publish: {exc}")

    state = load_state(state_path)
    if shopify is not None:
        from ecosystem.fulfillment import FulfilmentPolicy, process_orders
        try:
            summary["fulfilment"] = process_orders(
                state, shopify, cj_orders, gate,
                fulfilment_policy or FulfilmentPolicy.from_env(), now,
                save=lambda: save_state(state_path, state))
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"fulfilment: {exc}")
        save_state(state_path, state)
    try:
        summary["ads"] = apply_approvals(state, ads_adapters, gate, now)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"ads approvals: {exc}")
    save_state(state_path, state)

    copy_writer = AdCopyWriter(gateway)
    if ads_adapters and policy.auto_draft:
        for pid, prod in list(state["products"].items()):
            if not prod.get("url"):
                continue
            try:
                summary["drafted"] += draft_campaigns(
                    state, pid, ads_adapters, gate, copy_writer, policy,
                    reports_dir, now=now)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"draft {pid}: {exc}")
            save_state(state_path, state)

    if optimize:
        summary["guard"] = guard_campaigns(state, ads_adapters, gate, policy, now)
        save_state(state_path, state)
        perf: dict = {}
        article_spend = 0.0
        if shopify is not None and state["products"]:
            try:
                perf = product_performance(state, shopify)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"performance: {exc}")
            summary["seo"] = run_seo(state, shopify, now)
            summary["articles"], article_spend = draft_articles(state, shopify, gateway, now)
            save_state(state_path, state)
        summary["performance"] = perf
        if reports_dir is not None and (state["products"] or state["campaigns"]):
            other = [f"published {r['approval_id']} -> {r['status']}" for r in summary["published"]]
            other += [f"order {r['order']} -> {r['status']}" for r in summary["fulfilment"]]
            other += [f"ads {r['campaign']} -> {r['status']}" for r in summary["ads"]]
            other += [f"drafted {r['platform']} campaign -> {r['status']}" for r in summary["drafted"]]
            other += [f"error: {e}" for e in summary["errors"]]
            report = reports_dir / f"store_report_{now:%Y%m%d}.md"
            write_store_report(report, now, perf, state, summary["guard"], summary["seo"],
                               summary["articles"], other,
                               copy_writer.spend_usd + article_spend)
            summary["report_path"] = str(report)
    return summary

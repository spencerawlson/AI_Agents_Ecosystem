"""Ventoryn dashboard: FastAPI app.

Public front door:
  GET /                    landing page (public, no sign-in) — what
                           Ventoryn is, the URL box + free preview, $20/mo

Customer pages (sign-in required; each account sees ONLY its own data):
  GET /ops                 Live Ops — the post-sign-in home for every
                           account, scoped to that account's sites
                           (/game redirects to /ops, 303)
  GET /portfolio           the account's own sites/businesses
  GET /approvals           the account's own approvals (approve before
                           anything ships — nothing publishes unapproved)
  GET /report              the account's own growth report
  GET /account/...         the SaaS shell (see ventoryn.saas_routes)

Operator tools (operator account only, by direct URL):
  GET /businesses, /businesses/{id}, /experiments, /experiment-001,
  GET /reports, /reports/{name}
  POST /api/tick, /api/reset

Admin (admin password only — a signed-in Google session alone
never opens it; the password comes from VENTORYN_ADMIN_PASSWORD,
see docs/ventoryn-saas-shell.md):
  GET /admin               live now + recent activity (operator home)
  GET /admin/login         password-only login page
  GET /admin/subscribers   who subscribed
  GET /admin/accounts      all accounts on the platform
  GET /admin/agents        agent activity, health, problems (aggregates
                           only — never a customer's portfolio or
                           approvals, and no approve-on-behalf)

The operator account is the account whose email matches
SAAS_OPERATOR_EMAIL (see ventoryn.saas.SaasConfig). Spencer signs up
like any customer; his existing ecosystem businesses are attached
to that account by that email — the mechanism takes the email from
env config, it is never hardcoded.
"""

from __future__ import annotations

import html
import hmac
import json
import os
import re
import threading
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                              RedirectResponse)

from core.audit import AuditLog
from ventoryn.qrcode import qr_svg
from core.countries import country_options
from core.experiments import ExperimentEngine
from core.ledger import Ledger
from core.registry import BusinessRegistry
import urllib.parse

from ventoryn.saas import (ADMIN_SESSION_COOKIE, ADMIN_SESSION_TTL_HOURS,
                       CREDIT_COSTS, LIVE_WINDOW_MINUTES, SESSION_COOKIE)
from ventoryn.totp import provisioning_uri
from ventoryn import site_jobs, ui
from ventoryn.icons import icon
from ventoryn.social_costs import estimate_cost, rate_note
from ventoryn.ui import money
from orchestrator.approvals import ApprovalGate
from orchestrator.engine import Orchestrator


def esc(value: object) -> str:
    """HTML-escape any value interpolated into templates."""
    return html.escape(str(value), quote=True)


# Labels for the admin's recent-activity feed (ventoryn.saas kinds).
_ACTIVITY_KIND_LABELS = {
    "sign-in": "Signed in",
    "site-added": "Website added",
    "subscribed": "Subscribed",
    "job": "Job run",
}


def _ago(iso: str | None) -> str:
    """Human "last seen" for the admin's live list: "just now",
    "2 min ago"."""
    if not iso:
        return "—"
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return "just now"
    return f"{int(secs // 60)} min ago"


# Events that describe one agent run — as opposed to summaries like
# tick_completed, which carry no cost and say nothing about a run.
_RUN_EVENTS = frozenset({
    "run_completed", "run_failed", "task_verified", "task_rework_requested",
})
# When one run produced several events for it, take its cost/tokens
# from the first of these that carries any — run_completed first,
# since the worker records the run's own figures there.
_COST_PREFERENCE = (
    "run_completed", "task_verified", "run_failed", "task_rework_requested",
)


def _is_completion(event) -> bool:
    """A run completed only if it has a completion event: run_completed,
    or a task_verified that actually passed. A failed verification or a
    rework is work done, not a task completed."""
    if event.event == "run_completed":
        return True
    if event.event == "task_verified":
        return bool((event.result or {}).get("passed"))
    return False


def agent_cost_windows(audit: AuditLog, now=None,
                       windows: tuple = (1, 7, 30)) -> dict:
    """Real AI spend per time window, from the audit log — deduped per run
    exactly like agent_stats_from_audit (one run = one cost, even when it
    logged run_completed AND task_verified), so the Overview card and the
    Agents page can never disagree. {days: {"cost", "tokens", "runs"}}."""
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    groups: dict[str, list] = {}
    for e in audit.all_events():
        if e.event in _RUN_EVENTS and (e.run_id or e.task_id):
            groups.setdefault(e.run_id or e.task_id, []).append(e)
    out = {d: {"cost": 0.0, "tokens": 0, "runs": 0} for d in windows}
    for group in groups.values():
        chosen = next((ev for pref in _COST_PREFERENCE for ev in group
                       if ev.event == pref and (ev.cost_usd or ev.tokens_used)),
                      group[0])
        at = chosen.created_at
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        for d in windows:
            if at >= now - timedelta(days=d):
                out[d]["cost"] += chosen.cost_usd
                out[d]["tokens"] += chosen.tokens_used
                out[d]["runs"] += 1
    return out


def agent_stats_from_audit(audit: AuditLog, agent_types: list) -> list[dict]:
    """Per-agent totals derived from the audit log — the same source the
    feed reads, so the bars, the feed and the ledger can never disagree.

    One agent run can produce several events for the same run (a
    run_completed from the worker AND a task_verified from the
    orchestrator, plus a rework on the way); they are deduped by run_id
    (task_id when run_id is absent) so each run counts as one task and
    its cost/tokens are counted once. A failed or reworked run with no
    completion counts its spend but not a completed task. Summary events
    (tick_completed) are not run events and never touch the bars.

    Returns the same shape the ops page already renders (type,
    tasks_completed, tokens_used, cost_usd, last_task), one row per
    registered agent type — zeros only when the log truly has nothing
    for that agent.
    """
    groups: dict[str, list] = {}
    order: list[str] = []
    events = audit.all_events()
    for e in events:
        if e.event not in _RUN_EVENTS:
            continue
        key = e.run_id or e.task_id
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)

    stats = {t: {"tasks": 0, "tokens": 0, "cost": 0.0, "last_task": ""}
             for t in agent_types}
    for key in order:
        group_events = groups[key]
        agent = group_events[0].agent_type
        if agent not in stats:
            continue
        chosen = None
        for pref in _COST_PREFERENCE:
            for ev in group_events:
                if ev.event == pref and (ev.cost_usd or ev.tokens_used):
                    chosen = ev
                    break
            if chosen is not None:
                break
        if chosen is None:
            chosen = group_events[0]
        s = stats[agent]
        if any(_is_completion(ev) for ev in group_events):
            s["tasks"] += 1
        s["tokens"] += chosen.tokens_used
        s["cost"] += chosen.cost_usd
    # last_task: the agent's latest run event, in log order. Same
    # filter as the grouping above: a run event with no run/task id
    # belongs to no run, so it says nothing about what an agent did.
    for e in events:
        if e.event not in _RUN_EVENTS or e.agent_type not in stats:
            continue
        if not (e.run_id or e.task_id):
            continue
        label = e.event
        if e.tokens_used:
            label += f" · {e.tokens_used:,} tokens"
        stats[e.agent_type]["last_task"] = label
    return [{
        "type": t,
        "tasks_completed": stats[t]["tasks"],
        "tokens_used": stats[t]["tokens"],
        "cost_usd": round(stats[t]["cost"], 4),
        "last_task": stats[t]["last_task"],
    } for t in agent_types]


# Light, clean styling shared by the /reports and /approvals pages —
# matches the /report page. Mobile-friendly, no dark ops-room styling.
# Shared base styles for every server-rendered page (ventoryn/ui.py).
_PAGE_CSS = ui.BASE_CSS


def _approval_summary(a, registry) -> str:
    """Human-readable HTML summary of an approval (not raw ids)."""
    d = a.details or {}
    when = ""
    if getattr(a, "requested_at", None):
        when = esc(str(a.requested_at).replace("T", " ")[:19])
    if a.action == "mission_report_approval":
        biz_name = d.get("business_name") or d.get("site_url") or "?"
        report_name = d.get("report_name") or ""
        link = (f"<br><a href='/reports/{esc(report_name)}'>View report</a>"
                if report_name else "")
        try:
            spend = f"${float(d.get('total_spend_usd')):.4f}"
        except (TypeError, ValueError):
            spend = "?"
        return (
            f"<strong>Mission report</strong> — {esc(d.get('mission', 'mission'))} "
            f"for <strong>{esc(biz_name)}</strong><br>"
            f"<span class='muted'>{esc(d.get('drafts_count', '?'))} drafts · "
            f"spend {spend} · requested {when}</span>{link}"
        )
    if a.action == "marketing_experiment":
        report_name = d.get("report_name") or ""
        link = (f"<br><a href='/reports/{esc(report_name)}'>View growth report</a>"
                if report_name else "")
        try:
            budget = float(d.get("budget_usd") or 0)
            budget_txt = "organic ($0)" if budget == 0 else f"<strong>${budget:,.2f}</strong>"
        except (TypeError, ValueError):
            budget_txt = "?"
        steps = "".join(f"<li>{esc(s)}</li>" for s in (d.get("actions") or [])[:6])
        return (
            f"<strong>Marketing experiment</strong> — {esc(d.get('name', '?'))} "
            f"for <strong>{esc(d.get('business_name', '?'))}</strong><br>"
            f"<span class='muted'>{esc(d.get('channel', ''))} · "
            f"{esc(d.get('duration_days', '?'))} days · budget {budget_txt} · "
            f"requested {when}</span><br>"
            f"<span class='muted'>Hypothesis: {esc(d.get('hypothesis', ''))}<br>"
            f"KPI: {esc(d.get('kpi', ''))} → {esc(d.get('target', ''))} · "
            f"stop if: {esc(d.get('stop_rule', ''))}</span>"
            + (f"<ul class='muted'>{steps}</ul>" if steps else "")
            + "<span class='muted'>Approving starts the experiment: agents draft "
              "its content for you to post. Nothing is published or spent "
              f"automatically.</span>{link}"
        )
    if a.action == "approve_supplier":
        report_name = d.get("report_name") or ""
        link = (f"<br><a href='/reports/{esc(report_name)}'>View supplier report</a>"
                if report_name else "")
        try:
            price = f"${float(d.get('price_usd')):.2f}"
            margin = f"{float(d.get('margin')):.0%}"
        except (TypeError, ValueError):
            price, margin = "?", "?"
        flags = d.get("flags") or []
        flag_txt = (f"<br><span class='muted'>check: {esc('; '.join(flags))}</span>"
                    if flags else "")
        return (
            f"<strong>Supplier #{esc(d.get('rank', '?'))}</strong> — "
            f"{esc(d.get('supplier_name', '?'))} for "
            f"<strong>{esc(d.get('product_name', '?'))}</strong><br>"
            f"<span class='muted'>score {esc(d.get('score', '?'))} · "
            f"price {price} · margin {margin} · requested {when}</span>"
            f"<br><span class='muted'>Approving publishes this product to "
            f"Shopify with this supplier. Approve one per product.</span>"
            f"{flag_txt}{link}"
        )
    if a.action == "supplier_order_over_cost":
        try:
            detail = (f"approved ${float(d.get('approved_cost_usd')):.2f} → now "
                      f"<strong>${float(d.get('current_cost_usd')):.2f}</strong>")
        except (TypeError, ValueError):
            detail = "?"
        return (
            f"<strong>Supplier cost went up</strong> — order "
            f"{esc(d.get('order_name', '?'))}: {esc(', '.join(d.get('products') or []))}<br>"
            f"<span class='muted'>{detail} · {esc(d.get('shipping_method', ''))} · "
            f"requested {when}</span><br><span class='muted'>Approve to place the CJ "
            f"order at the new cost. Reject to handle this order yourself.</span>"
        )
    if a.action == "launch_ad_campaign":
        report_name = d.get("report_name") or ""
        link = (f"<br><a href='/reports/{esc(report_name)}'>View ads report</a>"
                if report_name else "")
        heads = " · ".join(str(h) for h in (d.get("headlines") or [])[:3])
        try:
            budget = (f"${float(d.get('daily_budget_usd')):.2f}/day × "
                      f"{int(d.get('duration_days'))} days = "
                      f"<strong>${float(d.get('total_budget_usd')):.2f} max</strong>")
            ceiling = f"${float(d.get('max_cpa_usd')):.2f}"
        except (TypeError, ValueError):
            budget, ceiling = "?", "?"
        return (
            f"<strong>Launch {esc(str(d.get('platform', '?')).title())} ads</strong> — "
            f"{esc(d.get('product_title', '?'))}<br>"
            f"<span class='muted'>{budget} · {esc(', '.join(d.get('countries') or []))} · "
            f"cost-per-sale ceiling {ceiling} · requested {when}</span>"
            f"<br><span class='muted'>“{esc(d.get('primary_text', ''))}” — {esc(heads)}</span>"
            f"<br><span class='muted'>Campaign is built and PAUSED. Approving starts "
            f"spending; it auto-pauses if it loses money.</span>{link}"
        )
    if a.action == "ad_budget_increase":
        try:
            detail = (f"${float(d.get('current_daily_budget_usd')):.2f} → "
                      f"<strong>${float(d.get('new_daily_budget_usd')):.2f}/day</strong> · "
                      f"cost/sale ${float(d.get('cpa_usd')):.2f} vs ceiling "
                      f"${float(d.get('max_cpa_usd')):.2f} · ROAS {d.get('roas')} · "
                      f"{d.get('purchases')} sales")
        except (TypeError, ValueError):
            detail = "?"
        amount = f"${a.amount_usd:,.2f}" if a.amount_usd is not None else "?"
        return (
            f"<strong>Scale {esc(str(d.get('platform', '?')).title())} ads</strong> — "
            f"{esc(d.get('product_title', '?'))}<br>"
            f"<span class='muted'>{detail} · adds up to {amount} · requested {when}</span>"
        )
    biz_name = ""
    if a.business_id:
        b = registry.get(a.business_id)
        biz_name = b.name if b is not None else a.business_id
    amount = f"${a.amount_usd:,.2f}" if a.amount_usd is not None else "—"
    task = (f"task <code>{esc(a.task_id[:12])}…</code>"
            if a.task_id else "no task")
    return (
        f"<strong>{esc(a.action)}</strong>"
        + (f" — {esc(biz_name)}" if biz_name else "") + "<br>"
        f"<span class='muted'>{task} · amount {amount} · "
        f"requested by {esc(a.requested_by)} · {when}</span>"
    )


def create_app(
    orchestrator: Orchestrator | None = None,
    registry: BusinessRegistry | None = None,
    ledger: Ledger | None = None,
    experiments: ExperimentEngine | None = None,
    approvals: ApprovalGate | None = None,
    audit: AuditLog | None = None,
    saas_store=None,
    saas_config=None,
    runtime=None,
) -> FastAPI:
    app = FastAPI(title="AI Business Ecosystem")
    # The live Runtime behind /api/tick and /api/reset. Callers that
    # already built one (ecosystem.runtime.create_dashboard_app) pass it
    # in, so the app never builds a second, throwaway runtime.
    app.state.runtime_holder = {"rt": runtime} if runtime is not None else None
    state = {
        "orchestrator": orchestrator or Orchestrator(),
        "registry": registry or BusinessRegistry(),
        "ledger": ledger or Ledger(),
        "experiments": experiments or ExperimentEngine(),
        "approvals": approvals or ApprovalGate(),
        "audit": audit if audit is not None else AuditLog(),
    }
    app.state.ecosystem = state

    # -- Ventoryn accounts: who is looking, and what is theirs -----------
    # The SaaS store/config are resolved once here (not per route) so
    # every route below closes over the same store. Plain create_app()
    # stays hermetic: an in-memory store, config from env.
    from ventoryn.saas import SaasConfig, SaasStore

    _saas_store = saas_store or SaasStore(":memory:")
    _saas_config = saas_config or SaasConfig.from_env()
    app.state.saas_store = _saas_store
    app.state.saas_config = _saas_config

    def _current_account(request: Request):
        """The signed-in account from the session cookie, or None."""
        return _saas_store.get_account_by_session(
            request.cookies.get(SESSION_COOKIE, ""))

    def _activity_label(path: str) -> str:
        """What the admin's "Live now" list shows as the last action
        for a request path. Overview and /api/snapshot (the
        Overview's data source, read by DJINN too) count as
        Overview — that is how an open page stays live."""
        if path in ("/ops", "/game", "/api/snapshot"):
            return "Overview"
        if path.startswith("/portfolio"):
            return "Websites"
        if path.startswith("/campaigns"):
            return "Campaigns"
        if path.startswith("/agents"):
            return "Agents"
        if path.startswith("/analytics"):
            return "Analytics"
        if path.startswith("/integrations"):
            return "Integrations"
        if path.startswith("/customers"):
            return "Customers"
        if path.startswith("/approvals"):
            return "Approvals"
        if path.startswith("/report"):
            return "Report"
        if path.startswith("/account/subscribe"):
            return "Subscribe"
        if path.startswith("/account"):
            return "My account"
        if path.startswith("/admin"):
            return "Admin"
        return "Browsing"

    def _need_account(request: Request):
        """(account, None) or (None, redirect to the landing page)."""
        account = _current_account(request)
        if account is None:
            return None, RedirectResponse("/", status_code=303)
        return account, None

    def _need_operator(request: Request):
        """(account, None) or (None, redirect to the landing page) —
        the operator is the account whose email matches
        SAAS_OPERATOR_EMAIL. Anyone else, signed in or not, is sent
        to the landing page, never to an operator page."""
        account = _current_account(request)
        if account is None or not _saas_config.is_operator(
                account["email"]):
            return None, RedirectResponse("/", status_code=303)
        return account, None

    def _is_unclaimed_visible(account, biz) -> bool:
        """An unclaimed business (no owner yet) is shown to the
        operator only — so it can be claimed into an account."""
        return (not biz.owner_email and
                _saas_config.is_operator(account["email"]))

    def _visible_businesses(account) -> list:
        """Registry businesses this account manages: the ones whose
        owner_email is this account's email, plus — for the operator
        only — unclaimed ones, so they can be claimed from
        /businesses. No business is tied to an account by name or by
        config: ownership is recorded on the business itself, and a
        business owned by another account is never visible."""
        email = (account["email"] or "").strip().lower()
        return [b for b in state["registry"].list()
                if (b.owner_email or "") == email
                or _is_unclaimed_visible(account, b)]

    def _owned_business_ids(account) -> set:
        """Ids of the registry businesses this account manages."""
        return {b.id for b in _visible_businesses(account)}

    def _approval_visible(account, approval, owned=None) -> bool:
        """Whose approval is this? The owner of its business decides
        it. With no business, or a business this runtime's registry
        does not know (written by another process), there is no owner
        to ask — it is operator oversight."""
        owned = _owned_business_ids(account) if owned is None else owned
        if approval.business_id in owned:
            return True
        if (approval.business_id is None or
                state["registry"].get(approval.business_id) is None):
            return _saas_config.is_operator(account["email"])
        return False

    def _biz_row(b) -> dict:
        pnl = state["ledger"].pnl(b.id)
        return {
            "id": b.id, "name": b.name,
            "business_type": b.business_type,
            "status": b.status.value,
            "owner_email": b.owner_email,
            "revenue": round(pnl.revenue, 2),
            "total_costs": round(pnl.total_costs, 2),
            "net_profit": round(pnl.net_profit, 2),
        }

    def _credit_usd() -> float:
        """What one credit is worth: the plan price over the monthly
        allowance ($20 / 1,000 credits = $0.02)."""
        from ventoryn.saas import PLAN_PRICE_USD

        return PLAN_PRICE_USD / max(1, int(_saas_config.monthly_credits or 0))

    def _site_cost_usd(site_id: str) -> float:
        """A website's cost: the credits its agent work used, valued at
        the plan rate."""
        return round(_saas_store.site_credits_used(site_id) * _credit_usd(), 2)

    def _money_stats_html(account, *, first: str = "") -> str:
        """Revenue / Costs / Net profit for the account — the same
        totals on Websites and Overview. Hidden revenue reads "—" and
        is marked so the header Revenue switch can reveal it."""
        rows = ([_biz_row(b) for b in _visible_businesses(account)]
                + _site_business_rows(account))
        hidden = any(r["revenue"] is None for r in rows)
        revenue = sum(r["revenue"] for r in rows if r["revenue"] is not None)
        costs = sum(r["total_costs"] or 0 for r in rows)
        profit = sum(r["net_profit"] for r in rows if r["net_profit"] is not None)
        n = 2 if first else 1
        return (f"<div class='v-stats'>{first}"
                f"<div class='v-stat v-stat-{n}'><span class='v-stat-label'>Revenue</span>"
                f"<span class='v-stat-value' data-rev-total>"
                f"{'—' if hidden else money(revenue)}</span></div>"
                f"<div class='v-stat v-stat-{n + 1}'><span class='v-stat-label'>Costs</span>"
                f"<span class='v-stat-value'>{money(costs)}</span></div>"
                f"<div class='v-stat v-stat-3'><span class='v-stat-label'>Net profit</span>"
                f"<span class='v-stat-value' data-rev-profit>"
                f"{'—' if hidden else money(profit)}</span></div></div>")

    def _site_business_rows(account) -> list[dict]:
        """The account's own SaaS sites as business rows, so the
        portfolio, report and Live Ops render them like any other
        business. Revenue is real: the sum of recorded conversions
        for the site — but ONLY when the site's revenue display is
        ON (revenue_for_site). When the customer chose to hide it,
        revenue and net profit are None, and every surface that
        shows them renders "hidden" instead of a dollar figure.
        There is no per-site dollar cost in the model, so costs
        read $0 — the page says so where it matters."""
        rows = []
        for site in _saas_store.list_sites(account["id"]):
            rev = _saas_store.revenue_for_site(site["id"])
            revenue = (rev["revenue"]["total_cents"] / 100
                       if rev and rev["revenue"] else None)
            cost = _site_cost_usd(site["id"])
            rows.append({
                "id": f"site:{site['id']}",
                "name": site["host"] or site["url"],
                "url": site["url"],
                "business_type": "website",
                "status": "active",
                "revenue": (round(revenue, 2) if revenue is not None
                            else None),
                "total_costs": cost,
                "net_profit": (round(revenue - cost, 2) if revenue is not None
                               else None),
            })
        return rows

    def _account_json(account) -> dict:
        """What the Live Ops page needs to know about the viewer:
        who they are, whether they are the operator, and whether
        they are subscribed. Never a secret, never another account."""
        return {
            "email": account["email"],
            "is_operator": _saas_config.is_operator(account["email"]),
            "subscription_status": account["subscription_status"],
            "credits_balance": account["credits_balance"],
        }

    # -- navigation ---------------------------------------------------------
    # Signed-in pages share ONE shell (ventoryn.nav): the purple
    # sidebar + app header. No signed-in page builds its own
    # document or its own nav — the operator's agent & financial
    # report renders through _operator_page like every other page
    # (its old pill-row shell is gone). The sidebar never links
    # Experiments, Experiment 001, Daily plan, or "for website
    # owners" — those stay reachable by direct URL only.
    # Portfolio color — the Live Ops visual language, not the plain
    # grey stat cards above (those stay exactly as they are: /report
    # uses them, and /report must not change). Violet gradients on
    # the three stat cards, a per-business accent top border on the
    # business cards, same typography and spacing as Live Ops.
    _PORTFOLIO_CSS = """
/* Portfolio: KPI cards + business cards in the platform palette.
   The accent strip is the landing page's violet -> blue gradient. */
.v-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px; margin: 18px 0 10px; }
@media (max-width: 640px) { .v-stats { grid-template-columns: 1fr; } }
.v-stat { position: relative; overflow: hidden; border-radius: var(--v-radius);
  padding: 20px 20px 18px; background: var(--v-card);
  border: 1px solid var(--v-line); box-shadow: var(--v-shadow); }
.v-stat::before { content: ""; position: absolute; inset: 0 0 auto 0;
  height: 4px; }
/* The highlighted first stat card (Websites, Analytics, Customers):
   deep emerald. The sidebar and the rest of the brand stay navy. */
.v-stat-1 { background: linear-gradient(135deg, #064e3b, #065f46);
  border-color: transparent; color: #fff; }
.v-stat-1 .v-stat-label { color: #a7f3d0; }
.v-stat-2::before { background: linear-gradient(90deg, var(--v-violet), var(--v-blue)); }
.v-stat-3::before { background: linear-gradient(90deg, var(--v-green), var(--v-green-soft)); }
.v-stat-label { display: block; font-size: 12px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .06em; color: var(--v-muted); }
.v-stat-value { display: block; font-size: 22px; font-weight: 800;
  margin-top: 6px; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
.v-biz-grid { display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px; margin: 18px 0; }
/* Websites page: the site list scrolls, so 20+ sites never swamp it. */
.v-site-list { max-height: min(72vh, 860px); overflow: auto;
  overscroll-behavior: contain; padding: 2px 6px 2px 2px; margin: 12px 0 18px;
  border-radius: var(--v-radius); scrollbar-width: thin;
  scrollbar-color: var(--v-line) transparent; }
.v-site-list:focus-visible { outline: 2px solid var(--v-violet); outline-offset: 2px; }
.v-site-list { background: var(--v-card); border: 1px solid var(--v-line);
  box-shadow: var(--v-shadow); padding: 0; }
.v-site-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.v-site-table th { position: sticky; top: 0; z-index: 1; background: var(--v-card);
  text-align: left; font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .05em; color: var(--v-muted); padding: 12px 16px;
  border-bottom: 1px solid var(--v-line); white-space: nowrap; }
.v-site-table td { padding: 12px 16px; border-bottom: 1px solid var(--v-line);
  vertical-align: middle; white-space: nowrap; }
.v-site-table tbody tr:last-child td { border-bottom: 0; }
.v-site-table tbody tr:hover td { background: color-mix(in srgb, var(--v-violet) 6%, transparent); }
.v-site-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.v-site-name { font-weight: 700; }
.v-site-tools .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap; }
.v-site-tools { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.v-site-tools input { flex: 1 1 220px; max-width: 360px; padding: 9px 12px;
  border: 1px solid var(--v-line); border-radius: 10px; font: inherit;
  background: var(--v-card); color: inherit; }
.v-biz { border-radius: var(--v-radius); padding: 18px 20px;
  background: var(--v-card); border: 1px solid var(--v-line);
  border-top: 4px solid var(--v-violet-deep); box-shadow: var(--v-shadow); }
.v-accent-0 { border-top-color: var(--v-violet-deep); }
.v-accent-1 { border-top-color: var(--v-blue); }
.v-accent-2 { border-top-color: var(--v-violet); }
.v-accent-3 { border-top-color: var(--v-navy); }
.v-biz h3 { margin: 0 0 4px; font-size: 18px; }
.v-biz-url { color: var(--v-muted); margin: 0 0 12px; font-size: 13px; }
.v-biz-url .pill { text-decoration: none; margin-left: 4px; }
.v-biz dl { display: grid; grid-template-columns: 1fr auto; gap: 6px 12px;
  margin: 0; font-size: 14px; padding-top: 12px;
  border-top: 1px solid var(--v-line); }
.v-biz dt { color: var(--v-muted); }
.v-biz-funnel { margin: 12px 0 0; box-shadow: none; }
.v-biz-funnel td { font-size: 13px; padding: 7px 10px; }
.v-biz-open { margin: 10px 0 0; font-weight: 600; font-size: 14px; }
.v-biz h3 a { color: inherit; text-decoration: none; }
.v-biz h3 a:hover { color: var(--v-violet-deep); }
.v-biz dd { margin: 0; font-weight: 700; text-align: right;
  font-variant-numeric: tabular-nums; }
.v-biz-extra { margin: 12px 0 0; font-size: 14px;
  border-top: 1px solid var(--v-line); padding-top: 12px; }
/* Websites carousel (Overview): the same cards as the grid, one
   swipeable row instead — scroll-snap does the work natively on
   touch, the arrow buttons are the non-touch fallback. */
.v-carousel-wrap { position: relative; display: flex; align-items: center;
  gap: 8px; margin: 18px 0; }
.v-carousel { display: flex; gap: 14px; overflow-x: auto; min-width: 0;
  scroll-snap-type: x mandatory; scroll-behavior: smooth;
  padding: 2px 2px 14px; -webkit-overflow-scrolling: touch; }
.v-carousel > .v-biz { scroll-snap-align: start; flex: 0 0 300px;
  min-width: 0; margin: 0; }
.v-carousel-btn { flex: 0 0 auto; width: 36px; height: 36px;
  border-radius: 50%; border: 1px solid var(--v-line);
  background: var(--v-card); color: var(--v-violet-deep);
  display: inline-flex; align-items: center; justify-content: center;
  cursor: pointer; box-shadow: var(--v-shadow); padding: 0; }
.v-carousel-btn:hover { background: var(--v-violet-soft); }
@media (max-width: 640px) {
  .v-carousel > .v-biz { flex: 0 0 85%; }
  .v-carousel-btn { display: none; }
  .v-carousel-wrap { gap: 0; margin: 14px 0; }
}
/* Analyze box (Overview only — its one home): the same URL +
   market-country form as the landing page's free preview, in
   platform card styling. Nothing is published; the report is
   saved locally and shown here. */
#analyze-panel .analyze-row { display: flex; gap: 12px; flex-wrap: wrap;
  align-items: flex-end; }
#analyze-panel .analyze-field { display: flex; flex-direction: column;
  gap: 4px; flex: 1 1 180px; }
#analyze-panel .analyze-label { font-size: 13px; font-weight: 600;
  color: var(--v-muted); }
.analyze-field input, .analyze-field select { min-height: 44px; }
.analyze-status { margin: 12px 0 0; font-size: 14px; color: var(--v-muted); }
.analyze-status.error { color: var(--v-red); font-weight: 600; }
.analyze-scores { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
.analyze-score { background: var(--v-violet-soft);
  border-radius: var(--v-radius-sm); padding: 12px 16px; }
.analyze-score .label { font-size: 12px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .05em; color: var(--v-muted); }
.analyze-score .value { font-size: 22px; font-weight: 800; }
.analyze-score .value.na { color: var(--v-muted); }
.analyze-findings { margin: 8px 0; padding-left: 20px; font-size: 14px; }
.analyze-findings li { margin: 6px 0; }
.analyze-findings .sev { font-weight: 700; color: var(--v-violet-deep); }
.analyze-report-link { display: inline-block; margin-top: 10px;
  font-weight: 600; }
@media (max-width: 560px) {
  #analyze-panel .analyze-row { flex-direction: column; align-items: stretch; }
  /* .analyze-field's flex-basis (180px) sizes WIDTH in the desktop
     row — once the row above becomes a column, the same basis sizes
     HEIGHT instead and stretches each field to ~180px tall with
     mostly empty space. Reset it here. */
  #analyze-panel .analyze-field { flex: 1 1 auto; }
}
/* Live activity feed (Overview) + the events feed (Agents):
   one plain row per event — time, actor, event, ids, cost, result.
   No game colors, no HUD. */
.v-feed-row { display: flex; flex-wrap: wrap; gap: 4px 12px;
  align-items: baseline; padding: 10px 0;
  border-bottom: 1px solid var(--v-line); font-size: 14px; }
.v-feed-row:last-child { border-bottom: 0; }
.v-feed-time { color: var(--v-muted); font-variant-numeric: tabular-nums; }
.v-feed-actor { background: var(--v-violet-soft);
  color: var(--v-violet-deep); border-radius: 999px; padding: 2px 10px;
  font-size: 12px; font-weight: 700; }
.v-feed-event { font-weight: 700; }
.v-feed-ids { color: var(--v-muted); }
.v-feed-cost { color: var(--v-muted); margin-left: auto; }
.v-feed-result { flex-basis: 100%; color: var(--v-muted); }
.v-feed-voice { flex-basis: 100%; font-size: 14px; }
/* Overview: Agent costs (24 h / 7 d / 30 d). */
.v-cost-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px; }
.v-cost-cell { border: 1px solid var(--v-line); border-radius: var(--v-radius-sm);
  padding: 12px 14px; background: #faf9fe; display: flex;
  flex-direction: column; gap: 2px; min-width: 0; }
.v-cost-label { font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .05em; color: var(--v-muted); }
.v-cost-big { font-size: 22px; font-weight: 800; color: var(--v-ink);
  font-variant-numeric: tabular-nums; }
.v-cost-sub { font-size: 13px; color: var(--v-muted); overflow-wrap: anywhere; }
@media (max-width: 640px) { .v-cost-grid { grid-template-columns: minmax(0, 1fr); } }
/* Overview: the three customer counts as one compact card. */
.v-kpi-stack .v-kpi-row { display: flex; align-items: center;
  justify-content: space-between; gap: 16px; padding: 12px 0;
  border-top: 1px solid var(--v-line); }
.v-kpi-stack .v-kpi-row:first-of-type { border-top: 0; padding-top: 4px; }
.v-kpi-stack .v-kpi-row p { margin: 2px 0 0; font-size: 13px; }
.v-kpi-stack .big { font-size: 28px; line-height: 1; }
.v-feed-meta { flex-basis: 100%; color: var(--v-muted); font-size: 13px; }
/* Agent activity (Agents page): tasks + spend per agent, and
   activity over time. Plain HTML bars — platform styling, not the
   old game's SVG charts. */
.v-team-bar { display: flex; align-items: center; gap: 8px; margin: 8px 0;
  font-size: 14px; }
.v-team-label { flex: 0 0 110px; font-weight: 700; }
.v-team-track { flex: 1; height: 14px; background: var(--v-bg);
  border-radius: 999px; overflow: hidden; }
.v-team-fill { display: block; height: 100%; border-radius: 999px; }
.v-team-num { flex: 0 0 64px; text-align: right; font-weight: 700;
  font-variant-numeric: tabular-nums; }
.v-act { display: flex; align-items: flex-end; gap: 6px; height: 120px;
  margin: 12px 0 4px; }
.v-act-col { flex: 1; display: flex; flex-direction: column;
  align-items: center; gap: 4px; min-width: 0; }
.v-act-bar { width: 100%; max-width: 36px; background: var(--v-blue);
  border-radius: 4px 4px 0 0; min-height: 2px; }
.v-act-num { font-size: 12px; font-weight: 700; }
.v-act-day { font-size: 11px; color: var(--v-muted); }
"""
    # Admin look — a blend of the Kay Ayiti and ShipItAyiti admin
    # boards: Kay Ayiti's light board (pale blue-grey background,
    # white cards, radius 20px, red primary) with ShipItAyiti's
    # slate/navy headers and ringed rounded cards. Same rules as the
    # Kay Ayiti / ShipItAyiti boards: counts and tables only, never
    # a customer's portfolio or approvals.
    _REPORT_CSS = """
/* Customer Acquisition & Attribution report: progress ring, status
   donut, flow steps, trend chart. All SVG is hand-built; every
   figure comes from real store data. Base cards/buttons/forms come
   from ventoryn/ui.py; the flow steps are white platform cards
   with a purple accent, a green check badge when the step has
   real evidence, a grey dot when it does not. */
.v-report-grid { display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 16px 0; }
/* min-width: 0 so a wide table scrolls inside its card instead of
   stretching the column past the screen on phones. */
.v-report-grid > * { min-width: 0; }
.v-report-grid .card { margin: 0; }
/* The merged Results section on Overview: donut + campaign table
   side by side in one card, stacking on narrow screens. */
.v-results-pair { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px; margin: 16px 0; }
.v-results-pair > * { min-width: 0; }
@media (max-width: 760px) { .v-results-pair { grid-template-columns: minmax(0, 1fr); } }
.v-progress { text-align: center; }
.v-bar-track { height: 12px; background: var(--v-violet-soft);
  border-radius: 999px; overflow: hidden; margin: 12px 0; }
.v-bar-fill { height: 100%; background: linear-gradient(90deg,
  var(--v-violet), var(--v-violet-deep)); border-radius: 999px; }
.v-flow { display: flex; gap: 6px; flex-wrap: wrap; align-items: stretch;
  margin: 12px 0; }
.v-flow-step { flex: 1 1 140px; border: 1px solid var(--v-line);
  border-radius: var(--v-radius); padding: 12px; background: #fff;
  box-shadow: var(--v-shadow); }
.v-flow-step.done { border-color: var(--v-green); background: var(--v-green-soft); }
.v-flow-step strong { display: block; font-size: 14px; margin-top: 4px; }
.v-flow-step span { font-size: 13px; color: var(--v-muted); }
.v-flow-check { display: inline-flex; align-items: center;
  justify-content: center; width: 26px; height: 26px; border-radius: 50%;
  background: var(--v-green); color: #fff; }
.v-flow-check svg { width: 16px; height: 16px; display: block; }
.v-flow-step:not(.done) .v-flow-check { background: var(--v-line);
  color: var(--v-faint); }
.v-flow-arrow { align-self: center; color: var(--v-violet-deep); }
.v-flow-arrow svg { width: 18px; height: 18px; display: block; }
.v-donut-wrap { display: flex; gap: 16px; align-items: center;
  flex-wrap: wrap; }
.v-legend { list-style: none; padding: 0; margin: 0; }
.v-legend li { margin: 6px 0; font-size: 14px; }
.v-dot { display: inline-block; width: 12px; height: 12px;
  border-radius: 50%; margin-right: 6px; }
.v-trend { width: 100%; height: auto; display: block; }
.v-bbar { display: flex; align-items: center; gap: 8px; margin: 8px 0;
  font-size: 14px; }
.v-bbar-label { flex: 0 0 110px; }
.v-bbar-track { flex: 1; height: 14px; background: var(--v-bg);
  border-radius: 999px; overflow: hidden; }
.v-bbar-fill { display: block; height: 100%; border-radius: 999px; }
.v-bbar-num { flex: 0 0 32px; text-align: right; font-weight: bold; }
.card form label { display: block; margin: 12px 0 4px; font-weight: 600; }
@media (max-width: 640px) {
  .v-report-grid { grid-template-columns: minmax(0, 1fr); }
}
/* Operator Agent & Financial report: the stat-card grid (Revenue /
   Total costs / Net profit). Token-only, like everything above. */
.cards { display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px;
  margin: 14px 0; }
.cards .card { margin: 0; }
.cards .card .label { font-size: 12px; font-weight: 700;
  color: var(--v-muted); text-transform: uppercase; letter-spacing: .05em; }
.cards .card .value { font-size: 24px; font-weight: 800;
  margin-top: 4px; font-variant-numeric: tabular-nums; }
"""

    # Approvals: the pending/decided tables as platform cards —
    # the same white-card language as /report. The decide flow
    # (POST /approvals/{id}/decide, redirect with ?decided=, the
    # Decided section) is unchanged.
    _APPROVALS_CSS = """
/* Approvals: platform cards around the pending / decided tables.
   The tables themselves keep the shared table look (ui.BASE_CSS);
   inside a card the outer border is dropped so the card is the
   only frame. */
.v-approvals .card table { margin: 0; border: 0; background: transparent; }
.v-approvals .card h2 { margin-top: 0; }
"""

    _ADMIN_CSS = """
/* Admin board: same platform palette as every other page. */
.v-admin-card { background: var(--v-card); border: 1px solid var(--v-line);
  border-radius: var(--v-radius); padding: 18px 20px; margin: 14px 0;
  box-shadow: var(--v-shadow); }
.v-admin-card h2 { margin: 0 0 10px; font-size: 18px; }
.v-admin-card h2 .v-count { color: var(--v-violet-deep); }
.v-admin-card .v-live-dot { display: inline-block; width: 9px; height: 9px;
  border-radius: 50%; background: var(--v-green); margin-right: 6px;
  box-shadow: 0 0 0 3px var(--v-green-soft); }
.v-admin-card table { margin: 6px 0 4px; }
.v-admin .v-error { color: var(--v-red); font-weight: 700; }
.v-auth-card { max-width: 420px; margin: 56px auto; background: var(--v-card);
  border: 1px solid var(--v-line); border-radius: 18px; padding: 28px;
  box-shadow: 0 10px 40px rgba(11, 14, 30, .10); }
.v-auth-brand { font-size: 20px; font-weight: 800; color: var(--v-navy);
  margin-bottom: 8px; }
.v-auth-brand::after { content: "."; color: var(--v-violet); }
.v-auth-card h1 { margin: 0 0 6px; font-size: 22px; }
.v-auth-card input[type=password], .v-auth-card input[type=text] {
  min-height: 48px; font-size: 17px; }
.v-auth-card button { width: 100%; margin-top: 16px; }
.v-admin-card .qr { background: #fff; border: 1px solid var(--v-line);
  border-radius: 12px; padding: 12px; display: inline-block;
  margin: 12px 0; }
.v-admin-card .codes { font-family: ui-monospace, Consolas, monospace;
  font-size: 17px; line-height: 1.9; background: #faf9fe;
  border: 1px solid var(--v-line); border-radius: 10px; padding: 12px 16px; }
.v-admin-foot { text-align: center; margin: 28px 0 8px; font-size: 14px;
  color: var(--v-muted); }
.v-admin-foot a { color: var(--v-muted); }
"""

    # Old _operator_page "current" keys -> platform sidebar keys.
    # /portfolio is Websites (its own item); /businesses is
    # reachable from Websites, so it deliberately gets the sidebar
    # with nothing active, and so does /reports (reachable from
    # Analytics). /report is a deep link under Analytics, not a
    # sidebar item. /experiments + /experiment-001 are direct-URL
    # operator tools: nothing active there by design, not an
    # oversight.
    _PLATFORM_CURRENT = {
        "live-ops": "overview", "portfolio": "websites",
        "businesses": None,
        "approvals": "approvals", "reports": None,
        "report": "analytics",
        "experiments": None, "experiment-001": None, "account": "my-account",
        "campaigns": "campaigns", "agents": "agents",
        "analytics": "analytics", "integrations": "integrations",
        "customers": "customers",
    }

    def _operator_page(title: str, inner: str, account=None,
                       current: str | None = "live-ops",
                       is_operator: bool = False,
                       extra_css: str = "") -> str:
        """Shell for the signed-in pages: the platform shell (purple
        sidebar + app header) + content, mobile-first, the same light
        styling as the Live Ops page. Every caller passes its
        account, so every signed-in page gets the sidebar; with no
        account the old centered rendering is kept as a fallback —
        no sidebar, no header — so nothing crashes."""
        if account is None:
            return ui.page(title, inner, extra_css=extra_css)
        from ventoryn.nav import platform_head_html, sidebar_html

        # Growth: verified count over the account's counted sites —
        # the exact computation from the /report customer branch.
        sites = _saas_store.list_sites(account["id"])
        counted_ids = [s["id"] for s in sites if s.get("count_show", 1)]
        verified = _saas_store.conversion_status_counts(
            counted_ids)["verified"]
        # Pending: per-account pending approvals — the filter from
        # the /approvals handler.
        owned = _owned_business_ids(account)
        pending = len([a for a in state["approvals"].pending()
                       if _approval_visible(account, a, owned)])
        # Social drafts awaiting this account's approval count toward
        # the same Approvals badge — one number, one page.
        pending += sum(
            1 for s in sites
            for d in _saas_store.list_social_drafts(s["id"])
            if d.get("status") == "pending")
        platform_current = _PLATFORM_CURRENT.get(current)
        sidebar = sidebar_html(
            platform_current, is_operator, verified=verified,
            pending_approvals=pending,
            account_email=account.get("email", ""))
        # The slim pause pill in the header row, Overview only,
        # operator only: the full Pause card lives in My account.
        pause_pill = ""
        if current == "live-ops" and is_operator:
            pause_pill = ui.pause_pill_html(
                _saas_store.get_global_pause(), "/ops")
        head = platform_head_html(title, pending_approvals=pending,
                                  pause_pill=pause_pill)
        return ui.page(title, inner, sidebar=sidebar, page_head=head,
                       extra_css=extra_css)

    # -- public landing page ----------------------------------------------
    # GET / is the front door: what Ventoryn is, the URL box + free
    # preview, the $20/month price, and sign-in with Google — all in
    # one place. No sign-in needed to read it; no internal data on
    # it (no business names, revenue, costs, agent internals, no
    # Live Ops content); no links to /ops, /game or /portfolio for
    # a visitor who is not signed in. A signed-in visitor gets a
    # "Continue to Live Ops" button — /ops is their home.
    _LANDING_CSS = """
/* Ventoryn landing page — premium AI SaaS (consolidated brief §2).
   Deep navy / near-black hero, electric violet + blue gradients,
   soft glass accents, clean white content sections. Vector only:
   every graphic is inline SVG or CSS — no bitmaps anywhere. */
:root { --navy: #0b0e1e; --navy-2: #12172e; --violet: #8b5cf6;
  --violet-soft: #c4b5fd; --violet-deep: #6d28d9; --blue: #3b82f6;
  --ink: #1a1a2e; --muted: #55556e; --line: #e5e0f5; --bg: #ffffff;
  --bg-tint: #f6f4ff; }
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial,
  sans-serif; margin: 0; overflow-x: hidden; color: var(--ink);
  background: var(--bg); line-height: 1.55; }
a { color: var(--violet-deep); }
.wrap { max-width: 1080px; margin: 0 auto; padding: 0 20px; }
.muted { color: var(--muted); font-size: 14px; }
.btn { display: inline-block; min-height: 44px; line-height: 44px;
  padding: 0 22px; margin: 4px 4px 4px 0; border-radius: 10px;
  border: 1px solid #ccc; background: #fff; color: #222; font-size: 16px;
  cursor: pointer; text-decoration: none; text-align: center; }
.btn-primary { background: var(--violet-soft); border-color: var(--violet-soft);
  color: #111; font-weight: bold; }
.btn-dark { background: var(--navy); border-color: var(--navy); color: #fff;
  font-weight: bold; }
.btn-ghost { background: transparent; border-color: rgba(255,255,255,.45);
  color: #fff; }
.btn-google { background: #fff; border-color: #888; }
/* -- sticky public nav (§3) -- */
.site-nav { position: sticky; top: 0; z-index: 50; background: var(--navy);
  color: #fff; }
.site-nav .wrap { display: flex; align-items: center; gap: 16px;
  min-height: 60px; }
.brand { font-size: 22px; font-weight: 800; letter-spacing: .02em;
  color: #fff; text-decoration: none; white-space: nowrap; }
.brand span { color: var(--violet-soft); }
.nav-links { display: flex; gap: 4px; margin: 0 auto; list-style: none;
  padding: 0; }
.nav-links a { color: #d9d4f5; text-decoration: none; font-size: 15px;
  padding: 10px 12px; border-radius: 8px; min-height: 44px;
  display: inline-flex; align-items: center; }
.nav-links a:hover { color: #fff; background: rgba(255,255,255,.08); }
.nav-item { position: relative; }
.nav-drop-btn { background: none; border: 0; color: #d9d4f5; font: inherit;
  font-size: 15px; padding: 10px 12px; border-radius: 8px; min-height: 44px;
  cursor: pointer; display: inline-flex; align-items: center; gap: 4px; }
.nav-drop-btn:hover { color: #fff; background: rgba(255,255,255,.08); }
.nav-drop { display: none; position: absolute; top: 100%; left: 0;
  background: var(--navy); border: 1px solid rgba(255,255,255,.14);
  border-radius: 10px; padding: 6px; min-width: 210px;
  box-shadow: 0 12px 30px rgba(0,0,0,.4); z-index: 20; }
.nav-item.open .nav-drop { display: block; }
.nav-drop a { display: block; padding: 10px 12px; }
.nav-actions { display: flex; gap: 8px; align-items: center;
  margin-left: auto; }
.nav-actions .btn { min-height: 40px; line-height: 40px; margin: 0;
  padding: 0 16px; font-size: 15px; }
.nav-toggle { display: none; background: none; border: 1px solid
  rgba(255,255,255,.35); border-radius: 8px; color: #fff; min-width: 44px;
  min-height: 44px; font-size: 22px; cursor: pointer; margin-left: 0; }
.nav-toggle svg { display: block; margin: auto; }
/* -- hero (§4) — two columns on desktop: copy left, dashboard
   preview right (reference image 1). -- */
.hero { background: radial-gradient(1100px 520px at 75% -10%, rgba(139,92,246,.45),
  transparent 60%), radial-gradient(900px 480px at 10% 110%, rgba(59,130,246,.35),
  transparent 55%), linear-gradient(180deg, var(--navy) 0%, var(--navy-2) 100%);
  color: #fff; padding: 56px 0 64px; }
.hero .wrap { max-width: 1280px; }
.hero-grid { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr);
  gap: 44px; align-items: start; }
.hero-copy { min-width: 0; text-align: left; }
.hero h2.sub { text-align: left; }
.hero p.lede { text-align: left; }
.url-card h2 { text-align: left; }
.eyebrow { display: inline-block; background: rgba(196,181,253,.16);
  border: 1px solid rgba(196,181,253,.45); color: var(--violet-soft);
  border-radius: 999px; padding: 6px 16px; font-weight: bold; font-size: 13px;
  letter-spacing: .08em; text-transform: uppercase; }
.hero h1 { font-size: 54px; line-height: 1.05; margin: 20px 0 10px;
  letter-spacing: -.01em; }
.grad { background: linear-gradient(90deg, #c4b5fd, #8b5cf6);
  -webkit-background-clip: text; background-clip: text; color: transparent; }
.hero h2.sub { font-size: 24px; line-height: 1.25; font-weight: 600;
  margin: 0 0 16px; color: #e6e2ff; }
.hero p.lede { font-size: 18px; line-height: 1.55; color: #c9c4e8;
  margin: 0 0 24px; max-width: 560px; }
.hero-ctas { display: flex; gap: 12px; flex-wrap: wrap; margin: 8px 0 20px; }
.hero-ctas .btn { min-height: 52px; line-height: 52px; padding: 0 28px;
  font-size: 17px; }
.trust { list-style: none; padding: 0; margin: 0 0 8px; display: flex; gap: 8px 24px;
  flex-wrap: wrap; color: #b9b3dd; font-size: 14px; }
.trust li { display: inline-flex; align-items: center; gap: 6px; }
.trust li svg { flex: 0 0 auto; }
.signed-banner { background: #ede9fe; border-bottom: 1px solid var(--line);
  padding: 10px 0; text-align: center; font-size: 15px; }
.signed-banner .btn { min-height: 40px; line-height: 40px; margin-left: 8px; }
/* -- URL card (the free preview — the functional entry point) -- */
.url-card { background: #fff; color: var(--ink); border-radius: 16px;
  padding: 20px; margin: 24px 0 0; text-align: left;
  box-shadow: 0 12px 40px rgba(0,0,0,.35); }
.url-card h2 { margin: 0 0 4px; font-size: 22px; color: var(--ink); }
.url-card .muted { color: var(--muted); }
.url-card label { display: block; margin: 14px 0 4px; font-weight: bold;
  color: var(--ink); }
.url-card input[type=text], .url-card select { width: 100%; min-height: 52px;
  padding: 8px 14px; font-size: 16px; border: 1px solid var(--line);
  border-radius: 10px; background: #fff; color: #111; }
.url-card input[type=text]:focus, .url-card select:focus { outline: none;
  border-color: var(--violet); }
.url-card .btn-primary { width: 100%; margin-top: 16px; }
#landing-status { color: var(--muted); margin-top: 10px; }
#landing-preview { background: #fff; color: var(--ink); border-radius: 12px;
  padding: 16px; margin-top: 16px; text-align: left; }
#landing-cta { margin-top: 12px; }
#landing-cta .muted { color: var(--muted); }
/* -- sections -- */
section { padding: 64px 0; }
section h2 { font-size: 34px; line-height: 1.2; margin: 0 0 12px;
  text-align: center; letter-spacing: -.01em; }
section .lede { font-size: 18px; color: var(--muted); text-align: center;
  max-width: 680px; margin: 0 auto 36px; }
section.alt { background: var(--bg-tint); }
.card { border: 1px solid var(--line); border-radius: 16px; padding: 20px;
  margin: 20px 0; background: #fff;
  box-shadow: 0 2px 10px rgba(109, 40, 217, 0.06); }
.card h2 { margin-top: 0; font-size: 22px; color: var(--ink); }
.big { font-size: 36px; font-weight: bold; color: var(--violet-deep); }
/* -- dashboard preview (§5) — reference images 1 + 2: the hero keeps
   image 1's navy background and copy; the preview inside it is rebuilt
   in image 2's clean light-panel dashboard style (dark mini sidebar +
   white cards on a pale background), carrying Ventoryn's acquisition
   content — NOT image 2's multi-business content. -- */
.preview { border: 1px solid #d9d2f2; border-radius: 20px; overflow: hidden;
  background: #fff; box-shadow: 0 24px 60px rgba(11,14,30,.18);
  text-align: left; min-width: 0; }
.preview-bar { display: flex; align-items: center; gap: 8px;
  background: var(--navy); color: #fff; padding: 12px 16px; }
.preview-bar .dot { width: 12px; height: 12px; border-radius: 50%; }
.preview-bar .title { margin-left: 8px; font-weight: 700; font-size: 15px; }
.preview-bar .meta { margin-left: auto; display: flex; align-items: center;
  gap: 10px; font-size: 13px; color: #d9d4f5; }
.preview-bar .demo-tag { background: rgba(196,181,253,.2);
  border: 1px solid rgba(196,181,253,.5); color: var(--violet-soft);
  font-size: 12px; font-weight: bold; border-radius: 999px; padding: 3px 10px; }
.preview-bar .range { background: rgba(255,255,255,.08);
  border: 1px solid rgba(255,255,255,.2); border-radius: 8px; padding: 4px 10px;
  font-size: 12px; display: inline-flex; align-items: center; gap: 4px; }
.preview-bar .avatar { width: 28px; height: 28px; border-radius: 50%;
  background: var(--violet); color: #fff; display: inline-flex;
  align-items: center; justify-content: center; font-size: 13px;
  font-weight: bold; }
.preview-body { display: flex; align-items: stretch; }
.preview-side { flex: 0 0 168px; background: var(--navy-2); color: #b9b3dd;
  padding: 14px 10px; font-size: 13px; }
.side-item { display: flex; align-items: center; gap: 8px; padding: 9px 10px;
  border-radius: 8px; margin: 2px 0; }
.side-item.active { background: rgba(139,92,246,.35); color: #fff;
  font-weight: 600; }
.side-item .badge { margin-left: auto; background: var(--violet); color: #fff;
  border-radius: 999px; font-size: 11px; font-weight: bold; padding: 1px 7px; }
.side-foot { margin-top: 16px; padding: 10px; border-top: 1px solid
  rgba(255,255,255,.12); font-size: 12px; color: #8f89b3; }
.preview-main { flex: 1; min-width: 0; background: #f4f2fb; padding: 16px;
  /* Dark text on the light panels: the hero sets white text, and
     without this the funnel numbers, queue titles, agent names and
     trace labels inherit white and vanish on the light background. */ color: var(--ink); }
.preview-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
.preview-cell { border: 1px solid #e5e0f5; border-radius: 12px;
  padding: 16px; background: #fff; box-shadow: 0 1px 4px rgba(11,14,30,.06); min-width: 0; }
.preview-cell h3 { margin: 0 0 10px; font-size: 15px; color: var(--ink); }
.preview-cell.wide { grid-column: 1 / -1; }
.ring-wrap { display: flex; gap: 16px; align-items: center; }
.ring-num { font-size: 28px; font-weight: 800; color: var(--violet-deep);
  line-height: 1.1; }
.ring-num small { display: block; font-size: 13px; color: var(--muted);
  font-weight: 600; }
.ring-sub { font-size: 13px; color: var(--muted); margin-top: 6px; }
.trend { margin-top: 12px; }
.trend svg { display: block; width: 100%; height: auto; }
.counter { font-size: 34px; font-weight: 800; color: var(--violet-deep); }
.counter small { font-size: 16px; color: var(--muted); font-weight: 600; }
.funnel-bar { display: flex; align-items: center; gap: 8px; margin: 6px 0;
  font-size: 13px; }
.funnel-bar .flabel { flex: 0 1 150px; min-width: 0; color: #333; }
.funnel-bar .ftrack { flex: 1; height: 12px; background: #e9e4fb;
  border-radius: 999px; overflow: hidden; }
.funnel-bar .ffill { height: 100%; background: linear-gradient(90deg,
  var(--violet), var(--blue)); border-radius: 999px; }
.funnel-bar .fnum { flex: 0 0 44px; text-align: right; font-weight: 700; }
.agent-row { display: flex; gap: 10px; align-items: flex-start; margin: 8px 0;
  font-size: 14px; }
.agent-row svg { flex: 0 0 28px; }
.agent-row b { display: block; font-size: 14px; }
.agent-row span { color: var(--muted); font-size: 13px; }
.queue-row { display: flex; gap: 10px; align-items: center; margin: 8px 0;
  font-size: 14px; background: #fff; border: 1px solid var(--line);
  border-radius: 10px; padding: 10px 12px; }
.queue-row .pill { margin-left: auto; flex: 0 0 auto; }
.pill { display: inline-block; font-size: 12px; font-weight: bold;
  border-radius: 999px; padding: 3px 10px; background: #ede9fe;
  color: var(--violet-deep); white-space: nowrap; }
.pill.green { background: #dcfce7; color: #166534; }
.pill.amber { background: #fef3c7; color: #92400e; }
.trace { display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  font-size: 13px; }
.trace .tstep { background: #fff; border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 10px; font-weight: 600; }
.trace .tarrow { color: var(--violet-deep); display: inline-flex;
  align-items: center; flex: 0 0 auto; }
.trace .tarrow svg { display: block; }
.demo-note { background: #fffbeb; border: 1px solid #fde68a; color: #92400e;
  border-radius: 10px; padding: 10px 14px; font-size: 13px; margin: 16px 0 0; }
.demo-revenue { font-size: 15px; }
.demo-revenue .hidden-amt { color: var(--muted); letter-spacing: .1em; }
/* -- steps (§6) -- */
.steps { list-style: none; padding: 0; margin: 0; display: grid;
  grid-template-columns: repeat(5, 1fr); gap: 28px; }
.steps li { background: #fff; border: 1px solid var(--line); border-radius: 14px;
  padding: 18px 16px; position: relative; }
/* Step connectors: an inline SVG arrow centred in the gap between
   cards. The old font-glyph arrow rendered as "92" here before;
   vector-only now. Hidden where the cards stack. */
.steps li:not(:last-child)::after { content: ""; position: absolute;
  right: -22px; top: 50%; transform: translateY(-50%); width: 16px;
  height: 16px; pointer-events: none;
  background: no-repeat center / contain url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 16 16'%3E%3Cpath d='M2 8h11M9 4.5L12.5 8 9 11.5' fill='none' stroke='%236d28d9' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E"); }
/* No connectors between step cards — the 5-card grid stands on its own
   (text arrows rendered as tofu here before; vector-only now). */
.step-icon { display: block; margin-bottom: 10px; }
.step-icon svg { display: block; }
.step-num { display: inline-block; width: 32px; height: 32px; border-radius: 50%;
  background: var(--violet-soft); color: #111; font-weight: bold;
  text-align: center; line-height: 32px; margin-bottom: 10px; }
.steps li strong { display: block; font-size: 16px; margin-bottom: 6px; }
.steps li span { font-size: 14px; color: var(--muted); line-height: 1.5; }
.steps-cta { text-align: center; margin-top: 28px; }
/* -- features (§7) -- */
.features { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.feature { border: 1px solid var(--line); border-radius: 14px; padding: 20px;
  background: #fff; box-shadow: 0 2px 10px rgba(109, 40, 217, 0.06); }
.feature svg { margin-bottom: 10px; }
.feature h3 { margin: 0 0 8px; font-size: 17px; color: var(--ink); }
.feature p { margin: 0; color: #444; font-size: 15px; line-height: 1.5; }
/* -- attribution (§8) -- */
.flow { display: flex; align-items: stretch; gap: 8px; flex-wrap: wrap;
  justify-content: center; margin: 24px 0; }
.flow .fstep { background: var(--navy); color: #fff; border-radius: 10px;
  padding: 12px 14px; font-size: 14px; font-weight: 600; text-align: center;
  max-width: 150px; }
.flow .farrow { align-self: center; color: var(--violet-deep);
  display: inline-flex; align-items: center; }
.flow .farrow svg { display: block; }
.rules { columns: 2; column-gap: 32px; max-width: 880px; margin: 0 auto;
  padding: 0; list-style: none; }
.rules li { break-inside: avoid; margin: 0 0 10px; font-size: 15px;
  color: #333; }
.rules li svg { vertical-align: -3px; margin-right: 6px; }
/* -- differentiation (§9) + who (§10) -- */
.principles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.principle { border-top: 4px solid var(--violet); background: #fff;
  border-radius: 0 0 14px 14px; padding: 20px; border-left: 1px solid var(--line);
  border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.principle h3 { margin: 0 0 8px; font-size: 18px; }
.principle p { margin: 0; color: #444; font-size: 15px; }
.who-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.who { border: 1px solid var(--line); border-radius: 14px; padding: 20px;
  background: #fff; }
.who h3 { margin: 0 0 6px; font-size: 17px; }
.who p { margin: 0; color: var(--muted); font-size: 14px; }
/* -- pricing (§11) -- */
.price-card { background: linear-gradient(135deg, #ede9fe, #ddd6fe);
  border: 2px solid var(--violet-soft); text-align: center; max-width: 640px;
  margin-left: auto; margin-right: auto; }
.price-card ul { list-style: none; padding: 0; margin: 16px auto;
  max-width: 460px; text-align: left; }
.price-card li { margin: 8px 0; font-size: 16px; }
.price-card li svg { vertical-align: -3px; margin-right: 6px; }
.credit-table { width: 100%; max-width: 460px; margin: 16px auto 0;
  border-collapse: collapse; font-size: 15px; background: #fff;
  border-radius: 10px; overflow: hidden; }
.credit-table th, .credit-table td { padding: 8px 12px; text-align: left;
  border-bottom: 1px solid var(--line); }
.credit-table th { background: var(--navy); color: #fff; font-size: 13px;
  text-transform: uppercase; letter-spacing: .04em; }
.credit-table td:last-child { text-align: right; font-weight: bold;
  color: var(--violet-deep); }
/* -- FAQ (§12) -- */
.faq { max-width: 760px; margin: 0 auto; }
.faq details { border: 1px solid var(--line); border-radius: 12px;
  padding: 16px 18px; margin: 10px 0; background: #fff; }
.faq summary { font-weight: 700; font-size: 16px; cursor: pointer;
  min-height: 44px; display: flex; align-items: center; }
.faq details p { margin: 10px 0 0; color: #444; font-size: 15px; }
/* -- final CTA (§13) -- */
.final-cta { background: radial-gradient(900px 400px at 70% 0%, rgba(139,92,246,.5),
  transparent 60%), linear-gradient(180deg, var(--navy), #0a0a18);
  color: #fff; text-align: center; }
.final-cta h2 { color: #fff; }
.final-cta p { color: #c9c4e8; font-size: 18px; max-width: 640px;
  margin: 0 auto 24px; }
/* -- footer (§14) -- */
.site-footer { background: var(--navy); color: #b9b3dd; padding: 40px 0 32px;
  font-size: 14px; }
.site-footer .cols { display: flex; gap: 32px; flex-wrap: wrap; }
.site-footer .col { min-width: 160px; }
.site-footer h4 { color: #fff; margin: 0 0 10px; font-size: 14px;
  text-transform: uppercase; letter-spacing: .06em; }
.site-footer a { color: #d9d4f5; text-decoration: none; display: block;
  padding: 4px 0; min-height: 28px; }
.site-footer a:hover { color: #fff; }
.site-footer .soon { color: #8f89b3; display: block; padding: 4px 0; }
.site-footer .base { margin-top: 28px; padding-top: 16px;
  border-top: 1px solid rgba(255,255,255,.12); text-align: center;
  color: #8f89b3; font-size: 13px; }
.score-row { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
.score { flex: 1 1 140px; border: 1px solid #ddd; border-radius: 8px;
  padding: 12px; background: #fafafa; text-align: center; }
.score .label { font-size: 12px; color: #666; text-transform: uppercase; }
.score .value { font-size: 24px; font-weight: bold; margin-top: 4px; }
.findings { margin: 8px 0; padding-left: 20px; }
.findings li { margin: 6px 0; }
@media (max-width: 900px) {
  .hero-grid { grid-template-columns: 1fr; }
  .steps { grid-template-columns: repeat(2, 1fr); }
  .steps li:not(:last-child)::after { display: none; }
  .features, .principles, .who-grid { grid-template-columns: 1fr 1fr; }
  .preview-grid { grid-template-columns: 1fr; }
  .rules { columns: 1; }
}
@media (max-width: 560px) {
  .hero { padding: 40px 0 36px; }
  .hero h1 { font-size: 34px; }
  .hero h2.sub { font-size: 21px; }
  .hero p.lede { font-size: 17px; }
  .btn { width: 100%; margin: 4px 0; }
  .hero-ctas .btn { width: 100%; }
  .nav-links { display: none; flex-direction: column; position: absolute;
    top: 60px; left: 0; right: 0; background: var(--navy); padding: 8px 16px 16px;
    gap: 0; }
  .nav-links.open { display: flex; }
  .nav-links a { padding: 12px; }
  .nav-item { width: 100%; }
  .nav-drop { position: static; box-shadow: none; border: 0; padding: 0 0 0 12px;
    min-width: 0; }
  .nav-toggle { display: block; }
  /* Sign In is the one nav-actions button with no other path on
     mobile (the hero's own text about Google sign-in has no link of
     its own) — keep it, drop the redundant Get Started (it only
     scrolls to the hero, already the first thing in view). */
  .nav-actions .btn { display: none; }
  .nav-actions #landing-signin { display: inline-block; }
  section { padding: 44px 0; }
  section h2 { font-size: 26px; }
  .steps, .features, .principles, .who-grid { grid-template-columns: 1fr; }
  .url-card { padding: 18px 16px; }
  .url-card input[type=text], .url-card select { font-size: 16px; }
  .preview-side { flex-basis: 96px; font-size: 11px; padding: 10px 6px; }
  .side-foot { font-size: 10px; }
  .preview-grid { padding: 12px; }
  .funnel-bar .flabel { flex-basis: 110px; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { animation: none !important;
    transition: none !important; }
}
"""
    def _landing_page(account) -> str:
        import json as _json

        signed_in = account is not None
        subscribed = bool(
            signed_in and account["subscription_status"] == "active")
        state_json = _json.dumps({"signedIn": signed_in,
                                  "subscribed": subscribed})
        # The credit table is rendered from the real CREDIT_COSTS —
        # never hardcoded, so the price page cannot drift from the
        # product. Labels are display names for the job keys.
        _credit_labels = {
            "analyze": "Analyze — site + GEO audit",
            "geo_audit": "GEO audit",
            "community_listening": "Community listening",
            "social_report": "Social report",
            "seo_article": "SEO article",
            "video_render": "Video render",
        }
        credit_rows = "".join(
            f"<tr><td>{_credit_labels.get(k, k)}</td>"
            f"<td>{CREDIT_COSTS[k]} credits</td></tr>"
            for k in ("analyze", "geo_audit", "community_listening",
                      "social_report", "seo_article", "video_render")
            if k in CREDIT_COSTS)
        if signed_in:
            signed_banner = (
                "<div class='signed-banner'><div class='wrap'>You are "
                f"signed in as {esc(account['email'])}. "
                "<a class='btn btn-primary' href='/ops'>"
                "Continue to Live Ops</a></div></div>")
            nav_signin = (
                "<a class='btn btn-google' id='landing-signin' "
                "href='/account/auth/google/start'>Sign In</a>")
            hero_signin_line = ""
        elif not _saas_config.google_configured:
            signed_banner = ""
            nav_signin = ("<span class='btn' id='landing-signin' "
                          "aria-disabled='true'>Sign In</span>")
            hero_signin_line = (
                "<p class='muted' style='color:#c9c4e8'>Sign-in with Google "
                "is not set up yet. The Google sign-in is configured by "
                "the operator (a Google Cloud OAuth client), not here — "
                "there is nothing to type in on this page.</p>")
        else:
            signed_banner = ""
            nav_signin = (
                "<a class='btn btn-google' id='landing-signin' "
                "href='/account/auth/google/start'>Sign In</a>")
            hero_signin_line = (
                "<p style='color:#c9c4e8;font-size:15px'>Sign in with "
                "Google — one account, no password to remember.</p>")
        json_ld = _json.dumps({
            "@context": "https://schema.org",
            "@type": "SoftwareApplication",
            "name": "Ventoryn",
            "applicationCategory": "BusinessApplication",
            "operatingSystem": "Web",
            "description": ("Ventoryn is an AI customer-acquisition "
                            "service. AI agents discover your "
                            "audience and prepare campaigns; you approve "
                            "every action; verified attribution shows "
                            "which efforts turn into paying customers."),
            "offers": {"@type": "Offer", "price": "20",
                       "priceCurrency": "USD",
                       "description": (f"$20/month, "
                                       f"{_saas_config.monthly_credits:,} "
                                       f"credits per month")},
            "url": _saas_config.base_url,
        })
        return f"""<!DOCTYPE html>
<html lang="en"><head><title>Ventoryn — Become a Founding Customer</title>
<meta charset="utf-8">
<meta name='viewport' content='width=device-width, initial-scale=1'>
<meta name="description" content="Ventoryn&#x27;s AI agents find your audience and prepare campaigns. You approve every action; verified attribution shows which efforts bring paying customers.">
<meta property="og:title" content="Ventoryn — Become a Founding Customer">
<meta property="og:description" content="Enter your website, connect your accounts, and let Ventoryn's AI agents discover your audience, prepare campaigns, and identify opportunities to grow. You approve every action.">
<meta property="og:type" content="website">
<meta property="og:url" content="{esc(_saas_config.base_url)}/">
<script type="application/ld+json">{json_ld}</script>
<style>{_LANDING_CSS}</style>{_own_snippet_landing()}</head><body>
<header class="site-nav"><div class="wrap">
<button class="nav-toggle" id="nav-toggle" aria-expanded="false" aria-controls="nav-links" aria-label="Open menu"><svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true"><path d="M3 5h16M3 11h16M3 17h16" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg></button>
<a class="brand" href="/">Ventoryn<span>.</span></a>
<nav aria-label="Product"><ul class="nav-links" id="nav-links">
<li class="nav-item"><button class="nav-drop-btn" id="nav-drop-btn" aria-expanded="false" aria-haspopup="true">Product <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
<div class="nav-drop" id="nav-drop">
<a href="#product">Features</a>
<a href="#start">Dashboard preview</a>
<a href="#who">Who it&#39;s for</a>
</div></li>
<li><a href="#how-it-works">How It Works</a></li>
<li><a href="#attribution">Attribution</a></li>
<li><a href="#pricing">Pricing</a></li>
<li><a href="#faq">FAQ</a></li>
</ul></nav>
<div class="nav-actions">{nav_signin}
<a class="btn btn-primary" href="#start">Get Started</a></div>
</div></header>
{signed_banner}
<main>
<section class="hero" id="start">
<div class="wrap hero-grid">
<div class="hero-copy">
<span class="eyebrow">AI-Powered Customer Acquisition</span>
<h1>Become a Ventoryn <span class="grad">Founding Customer</span>.</h1>
<h2 class="sub">Let AI Find Them. You Approve Every Move.</h2>
<p class="lede">Enter your website, connect your accounts, and let
Ventoryn&rsquo;s AI agents discover your audience, prepare campaigns, and
identify opportunities to grow. You approve every action, while verified
attribution shows which efforts turn into paying customers.</p>
<div class="hero-ctas">
<a class="btn btn-primary" href="#start">Start Getting Customers</a>
<a class="btn btn-ghost" href="#how-it-works">See How It Works</a>
</div>
<ul class="trust">
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#c4b5fd" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Human-approved actions</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#c4b5fd" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Verified customer attribution</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#c4b5fd" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Revenue privacy controls</li>
</ul>
{hero_signin_line}
<div class="card url-card">
<h2><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" style="vertical-align:-4px;margin-right:6px"><circle cx="11" cy="11" r="7" fill="none" stroke="#6d28d9" stroke-width="2"/><line x1="16.5" y1="16.5" x2="21" y2="21" stroke="#6d28d9" stroke-width="2" stroke-linecap="round"/></svg>Analyze your site — free preview</h2>
<p class="muted">The same analysis Ventoryn runs before it starts work:
your site score, your answer-engine (GEO) score, and the top things
to fix. Nothing is published.</p>
<form id="landing-analyze-form">
<label for="landing-url">Your website URL</label>
<input id="landing-url" type="text" inputmode="url"
 placeholder="yourwebsite.com" autocomplete="off" required>
<p class="muted">Just your domain works — type example.com, no https:// needed.</p>
<label for="landing-country">Market country</label>
<select id="landing-country" autocomplete="off" required>
{country_options("ca")}</select>
<p><button type="submit" class="btn btn-primary"
 id="landing-submit">Analyze my site — free</button></p>
</form>
<div id="landing-status" class="muted" aria-live="polite"></div>
<div id="landing-preview" hidden></div>
<div id="landing-cta" hidden></div>
</div>
</div>
<div class="preview" role="img" aria-label="Illustrative preview of the Ventoryn dashboard: verified customers, acquisition funnel, agent activity, approval queue, and a conversion attribution trace. All numbers are example data.">
<div class="preview-bar">
<span class="dot" style="background:#ff5f57"></span>
<span class="dot" style="background:#febc2e"></span>
<span class="dot" style="background:#28c840"></span>
<span class="title">Ventoryn — Live Ops</span>
<span class="meta">
<span class="demo-tag">ILLUSTRATIVE DEMO DATA</span>
<span class="range">Last 30 Days <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 22a2 2 0 002-2h-4a2 2 0 002 2zM6 9a6 6 0 0112 0c0 7 3 9 3 9H3s3-2 3-9z" fill="none" stroke="#d9d4f5" stroke-width="2"/></svg>
<span class="avatar">V</span>
</span>
</div>
<div class="preview-body">
<div class="preview-side" aria-hidden="true">
<div class="side-item active">Overview</div>
<div class="side-item">Campaigns</div>
<div class="side-item">Agents</div>
<div class="side-item">Approvals <span class="badge">3</span></div>
<div class="side-item">Attribution</div>
<div class="side-item">Integrations</div>
<div class="side-item">Settings</div>
<div class="side-foot">Growth Plan<br>Founding Customer Program</div>
</div>
<div class="preview-main">
<div class="preview-grid">
<div class="preview-cell">
<h3>Verified customers</h3>
<p>Verified customers appear here as signed sales land — each one
traced from an approved action to a Stripe checkout.</p>
<div class="trend"><svg viewBox="0 0 300 64" role="img" aria-label="Illustrative acquisition trend of verified customers over time"><polyline points="0,56 30,54 60,50 90,48 120,44 150,38 180,34 210,28 240,20 270,12 300,8" fill="none" stroke="#8b5cf6" stroke-width="2.5"/></svg></div>
</div>
<div class="preview-cell">
<h3>Acquisition funnel</h3>
<div class="funnel-bar"><span class="flabel">Audience Opportunities</span><span class="ftrack"><span class="ffill" style="display:block;width:81%"></span></span><span class="fnum">2,840</span></div>
<div class="funnel-bar"><span class="flabel">Approved Actions</span><span class="ftrack"><span class="ffill" style="display:block;width:21%"></span></span><span class="fnum">186</span></div>
<div class="funnel-bar"><span class="flabel">Published Actions</span><span class="ftrack"><span class="ffill" style="display:block;width:19%"></span></span><span class="fnum">154</span></div>
<div class="funnel-bar"><span class="flabel">Attributed Visits</span><span class="ftrack"><span class="ffill" style="display:block;width:100%"></span></span><span class="fnum">4,320</span></div>
<div class="funnel-bar"><span class="flabel">Signups</span><span class="ftrack"><span class="ffill" style="display:block;width:27%"></span></span><span class="fnum">312</span></div>
<div class="funnel-bar"><span class="flabel">Verified Customers</span><span class="ftrack"><span class="ffill" style="display:block;width:7%"></span></span><span class="fnum">24</span></div>
</div>
<div class="preview-cell">
<h3>Agent activity</h3>
<div class="agent-row"><svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><circle cx="12" cy="12" r="7" fill="none" stroke="#6d28d9" stroke-width="2"/><line x1="17" y1="17" x2="24" y2="24" stroke="#6d28d9" stroke-width="2"/></svg><div><b>Audience Research Agent</b><span>Identifying relevant communities</span></div><span class="pill green" style="margin-left:auto">Running</span></div>
<div class="agent-row"><svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><path d="M4 14h4l3-7 4 14 3-7h6" fill="none" stroke="#3b82f6" stroke-width="2"/></svg><div><b>Social Listening Agent</b><span>Monitoring conversations</span></div><span class="pill green" style="margin-left:auto">Running</span></div>
<div class="agent-row"><svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true"><rect x="5" y="4" width="18" height="14" rx="2" fill="none" stroke="#8b5cf6" stroke-width="2"/><line x1="9" y1="9" x2="19" y2="9" stroke="#8b5cf6" stroke-width="2"/><line x1="9" y1="13" x2="16" y2="13" stroke="#8b5cf6" stroke-width="2"/></svg><div><b>Content Agent</b><span>Preparing engagement proposals</span></div><span class="pill green" style="margin-left:auto">Running</span></div>
<div class="agent-row"><svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><path d="M14 3l9 4v6c0 6-4 9-9 11-5-2-9-5-9-11V7z" fill="none" stroke="#166534" stroke-width="2"/><path d="M10 14l3 3 5-6" fill="none" stroke="#166534" stroke-width="2"/></svg><div><b>Attribution Agent</b><span>Verifying conversions</span></div><span class="pill green" style="margin-left:auto">Running</span></div>
</div>
<div class="preview-cell">
<h3>Approval queue</h3>
<div class="queue-row">Review proposed Reddit post <span class="pill">Review</span></div>
<div class="queue-row">Approve X engagement draft <span class="pill green">Approve</span></div>
<div class="queue-row">Review community outreach opportunity <span class="pill">Review</span></div>
<p class="muted" style="margin:8px 0 0">Proposed actions — nothing is
published until you approve it.</p>
</div>
<div class="preview-cell wide">
<h3>Conversion attribution — example trace</h3>
<div class="trace"><span class="tstep">Approved Campaign</span><span class="tarrow"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 8h11M9 4.5L12.5 8 9 11.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span class="tstep">Signed Link</span><span class="tarrow"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 8h11M9 4.5L12.5 8 9 11.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span class="tstep">Website Visit</span><span class="tarrow"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 8h11M9 4.5L12.5 8 9 11.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span class="tstep">Stripe Checkout</span><span class="tarrow"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 8h11M9 4.5L12.5 8 9 11.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span class="tstep">Verified Customer</span> <span class="pill green">Verified</span></div>
<p class="muted" style="margin:10px 0 0">Attribution: Reddit Campaign #RD-4827 — <span class="pill green">Verified</span></p>
<p class="demo-revenue" style="margin:12px 0 0">Revenue for this example
customer: <span class="hidden-amt" id="demo-revenue-value">$••• hidden</span>
— revenue is private by default.
<button class="btn" id="demo-revenue-toggle" type="button" style="min-height:36px;line-height:36px;margin-left:8px">Reveal example revenue</button></p>
</div>
</div>
<p class="demo-note"><strong>Illustrative product preview — not live customer data.</strong> Every
number in this preview is an example to show how the dashboard reads —
not real customer data, not a result Ventoryn promises.</p>
</div>
</div>
</div>
</section>
<section id="how-it-works" class="alt">
<div class="wrap">
<h2>How <span class="grad">Ventoryn</span> Works</h2>
<p class="lede">From your website to verified paying customers — in 5 simple steps.</p>
<ol class="steps">
<li><span class="step-icon"><svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><circle cx="14" cy="14" r="11" fill="none" stroke="#6d28d9" stroke-width="2"/><path d="M3 14h22M14 3c3 3.5 3 18.5 0 22M14 3c-3 3.5-3 18.5 0 22" fill="none" stroke="#6d28d9" stroke-width="1.6"/></svg></span><span class="step-num">1</span><strong>Enter Your Website</strong><span>Tell Ventoryn where your business lives. We analyze your website, product positioning, and potential audience.</span></li>
<li><span class="step-icon"><svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><rect x="4" y="8" width="20" height="12" rx="2" fill="none" stroke="#3b82f6" stroke-width="2"/><path d="M10 14h8" stroke="#3b82f6" stroke-width="2"/></svg></span><span class="step-num">2</span><strong>Connect Your Accounts</strong><span>Securely connect supported social networks, analytics tools, and Stripe to bring your acquisition data together.</span></li>
<li><span class="step-icon"><svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><circle cx="12" cy="12" r="7" fill="none" stroke="#8b5cf6" stroke-width="2"/><line x1="17" y1="17" x2="24" y2="24" stroke="#8b5cf6" stroke-width="2"/></svg></span><span class="step-num">3</span><strong>Let AI Research Opportunities</strong><span>Ventoryn&rsquo;s agents find relevant audiences, conversations, communities, and content opportunities.</span></li>
<li><span class="step-icon"><svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><path d="M14 3l9 4v6c0 6-4 9-9 11-5-2-9-5-9-11V7z" fill="none" stroke="#166534" stroke-width="2"/><path d="M10 14l3 3 5-6" fill="none" stroke="#166534" stroke-width="2"/></svg></span><span class="step-num">4</span><strong>Approve Every Action</strong><span>Review recommended posts, replies, and campaigns before anything is published or executed.</span></li>
<li><span class="step-icon"><svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><path d="M6 25V4" stroke="#92400e" stroke-width="2"/><path d="M6 4h16l-4 5 4 5H6" fill="none" stroke="#92400e" stroke-width="2"/></svg></span><span class="step-num">5</span><strong>Track Verified Customers</strong><span>Follow attribution from approved action to checkout and see which approved actions turned into verified paying customers.</span></li>
</ol>
<div class="steps-cta"><a class="btn btn-primary" href="/founding-customer-waitlist">Join the Founding Customer Waitlist</a></div>
</div>
</section>
<section id="product">
<div class="wrap">
<h2>What Ventoryn does for your website</h2>
<p class="lede">AI does the research. You make the decisions. Nothing
goes live without your approval.</p>
<div class="features">
<div class="feature"><svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true"><circle cx="14" cy="14" r="8" fill="none" stroke="#6d28d9" stroke-width="2.5"/><line x1="20" y1="20" x2="28" y2="28" stroke="#6d28d9" stroke-width="2.5"/></svg><h3>AI Audience Discovery</h3><p>Relevant conversations, communities, and audience segments found from your website and offering — not from a bought list.</p></div>
<div class="feature"><svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true"><path d="M4 8h24v14H4z" fill="none" stroke="#3b82f6" stroke-width="2.5"/><path d="M4 8l16 9L4 8z" fill="none" stroke="#3b82f6" stroke-width="2.5"/></svg><h3>Smart Social Engagement</h3><p>Draft relevant Reddit and X content that respects platform rules, community guidelines — and your approval.</p></div>
<div class="feature"><svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true"><path d="M6 16h4l3-8 4 16 3-8h6" fill="none" stroke="#8b5cf6" stroke-width="2.5"/></svg><h3>Social Listening</h3><p>Monitor supported conversations and find genuine opportunities to help potential customers — where they already are.</p></div>
<div class="feature"><svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true"><path d="M16 3l11 5v8c0 7-5 11-11 13C10 27 5 23 5 16V8z" fill="none" stroke="#166534" stroke-width="2.5"/><path d="M11 16l4 4 7-8" fill="none" stroke="#166534" stroke-width="2.5"/></svg><h3>Approval-First Automation</h3><p>All proposed actions sit in a review queue. Never publish, reply, send outreach, or spend money before explicit authorization.</p></div>
<div class="feature"><svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true"><path d="M10 16l4 4 8-9" fill="none" stroke="#6d28d9" stroke-width="2.5"/><circle cx="16" cy="16" r="13" fill="none" stroke="#6d28d9" stroke-width="2.5"/></svg><h3>Verified Conversion Attribution</h3><p>Unique attribution identifiers with supported analytics and Stripe events, plus verification evidence. Verified attribution is distinguished from modeled or untracked conversions.</p></div>
<div class="feature"><svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true"><rect x="7" y="14" width="18" height="13" rx="2" fill="none" stroke="#92400e" stroke-width="2.5"/><path d="M11 14V9a5 5 0 0110 0v5" fill="none" stroke="#92400e" stroke-width="2.5"/></svg><h3>Revenue Privacy</h3><p>You control whether revenue amounts are displayed — and whether the customer count is displayed. They are separate settings. Private Stripe information is never exposed publicly.</p></div>
</div>
</div>
</section>
<section id="attribution" class="alt">
<div class="wrap">
<h2>Not Just Clicks. Real Paying Customers.</h2>
<p class="lede">Ventoryn connects approved marketing actions to actual
checkout events, giving you evidence of which activities contributed to
new paying customers.</p>
<div class="flow">
<span class="fstep">AI Recommendation</span><span class="farrow"><svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><path d="M3 10h13M11 5.5L15.5 10 11 14.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
<span class="fstep">Customer Approval</span><span class="farrow"><svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><path d="M3 10h13M11 5.5L15.5 10 11 14.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
<span class="fstep">Signed Campaign Link</span><span class="farrow"><svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><path d="M3 10h13M11 5.5L15.5 10 11 14.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
<span class="fstep">Website Visit</span><span class="farrow"><svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><path d="M3 10h13M11 5.5L15.5 10 11 14.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
<span class="fstep">Stripe Payment</span><span class="farrow"><svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><path d="M3 10h13M11 5.5L15.5 10 11 14.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
<span class="fstep">Verified Conversion</span>
</div>
<ul class="rules">
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Every approved action carries a unique, tamper-resistant attribution identifier, bound to the campaign and the action that was approved.</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Stripe webhook signatures are validated on every payment notice, and payment events are processed idempotently — a payment delivered twice counts once.</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>A customer is counted only after an eligible, successful payment event is verified.</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Refunds, disputes, and reversed payments are reconciled — a sale that is later refunded or a dispute that is lost is removed from the verified count automatically.</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Protection against replay, manipulation, and unauthorized reassignment of attribution.</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Consent, privacy, and tracking requirements are respected; cross-device and cross-session attribution is shown as limited when deterministic evidence is unavailable.</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Ventoryn never claims every customer can be attributed with certainty — where the evidence is missing, a sale is shown as Unattributed or Verification Pending.</li>
</ul>
</div>
</section>
<section id="differentiation">
<div class="wrap">
<h2>AI Does the Research. You Make the Decisions.</h2>
<p class="lede">Automation is not a substitute for authentic engagement.
Ventoryn is built on three principles.</p>
<div class="principles">
<div class="principle"><h3>Your Brand, Your Voice</h3><p>Recommendations are aligned with your business&rsquo;s positioning — your voice, not a generic template.</p></div>
<div class="principle"><h3>Nothing Goes Live Without Approval</h3><p>You control the content and every external action. Nothing is published, replied to, or sent without your explicit authorization.</p></div>
<div class="principle"><h3>Proof Over Vanity Metrics</h3><p>Verified conversions — not likes and impressions. The number that matters is paying customers.</p></div>
</div>
</div>
</section>
<section id="who" class="alt">
<div class="wrap">
<h2>Who Ventoryn is for</h2>
<p class="lede">Built for businesses looking for their first paying
customers.</p>
<div class="who-grid">
<div class="who"><h3>SaaS founders</h3><p>Your product is live. Your paying customers are not — yet.</p></div>
<div class="who"><h3>Independent software developers</h3><p>Ship the product, let Ventoryn find the people who will pay for it.</p></div>
<div class="who"><h3>Startup teams</h3><p>Small team, no marketing department — agents do the daily outreach work.</p></div>
<div class="who"><h3>Online service businesses</h3><p>Turn your website into a steady flow of new clients.</p></div>
<div class="who"><h3>Digital product creators</h3><p>Courses, templates, tools — find the buyers already looking.</p></div>
<div class="who"><h3>Founders launching their first website</h3><p>Day one on the web, with a plan to find paying customers.</p></div>
</div>
</div>
</section>
<section id="pricing">
<div class="wrap">
<h2>Pricing</h2>
<p class="lede">One plan. Everything included.</p>
<div class="card price-card">
<h2>$20/month</h2>
<p><span class="big">$20</span> / month</p>
<ul>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Your websites on Ventoryn</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>{_saas_config.monthly_credits:,} credits every month</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>A growth report per site</li>
<li><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.5 3.5L13 4.5" fill="none" stroke="#6d28d9" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Agents working daily to find new paying customers</li>
</ul>
<table class="credit-table">
<thead><tr><th>What a credit buys</th><th>Cost</th></tr></thead>
<tbody>{credit_rows}</tbody>
</table>
<p style="margin-top:16px"><a class="btn btn-primary" href="/account/subscribe">Subscribe — $20/month</a></p>
<p class="muted">No free trial. No guarantees on customer numbers.
Cancel anytime from your account.</p>
</div>
</div>
</section>
<section id="faq" class="alt">
<div class="wrap">
<h2>Frequently asked questions</h2>
<div class="faq">
<details><summary>What does Ventoryn do?</summary><p>Ventoryn is an AI customer-acquisition service for websites. You enter your website and connect your accounts; AI agents discover your audience, prepare campaigns, and identify opportunities — and you approve every action before anything happens. Progress is measured in verified paying customers, not page views.</p></details>
<details><summary>How does Ventoryn help me find customers?</summary><p>Its agents research where your audience already gathers — relevant communities, conversations, and search demand — then prepare outreach (posts, replies, articles, videos) for your review. Approved actions go out; everything else stays a draft.</p></details>
<details><summary>Will Ventoryn post without my approval?</summary><p>No. Nothing is ever published, replied to, or sent without your explicit approval. Every proposed action sits in your approval queue first. You pay for the service — you decide what goes out under your name.</p></details>
<details><summary>Which accounts can I connect?</summary><p>Per website: Reddit, X, Plausible (analytics), and Stripe (sales). Reddit and X posting need the platform OAuth app registrations first — until then the connection honestly says so. X&rsquo;s API is paid and rate-limited, so posting there is slower.</p></details>
<details><summary>How does Ventoryn verify paying customers?</summary><p>Every approved action carries a unique, signed attribution link. When a visitor follows it and pays through your Stripe checkout, the payment notice (signature-verified, processed once) is matched to that link — and only then is the customer counted as verified. A sale with no signature is reported as unattributed, never guessed.</p></details>
<details><summary>Do I need Stripe?</summary><p>To count verified paying customers, yes — Ventoryn reads payments from your Stripe account (read-only; it can never charge or refund). Without a Stripe connection, Ventoryn can still analyze your site and prepare campaigns, but paying customers show as unavailable until you connect it.</p></details>
<details><summary>Can I hide my revenue?</summary><p>Yes — revenue is private by default. You choose, per website, whether revenue amounts are displayed and whether the customer count is displayed. They are two separate settings. With revenue hidden, no dollar figure appears anywhere on Ventoryn — your sales still count as verified customers. Revenue figures never go into logs.</p></details>
<details><summary>Does Ventoryn guarantee new customers?</summary><p>No. Ventoryn does not guarantee a number of paying customers. What Ventoryn guarantees is the work: daily research, prepared campaigns, your approval on everything, and honest attribution of what actually turned into a paying customer.</p></details>
<details><summary>Can I disconnect my accounts?</summary><p>Yes, anytime, from your site&rsquo;s Connections section. Disconnecting revokes the token at the provider (where the provider allows it) and deletes the stored token and revenue data. Deleting your account does the same for everything it owns.</p></details>
<details><summary>How is my information protected?</summary><p>Two-factor authentication (TOTP) is mandatory for every account from the first sign-in — you set it up with an authenticator app and backup codes. Sign-in is zero-trust: every request is verified server-side against your session, sessions expire, and a logged-out session stops working immediately. Tokens Ventoryn holds are encrypted at rest, server-side only, with the minimum scopes for the job. Sensitive actions — disconnecting a connection, changing the revenue display, disconnecting Stripe — ask for a fresh two-factor code. The admin account is protected by its own password plus two-factor, separate from any customer sign-in.</p></details>
</div>
</div>
</section>
<section class="final-cta">
<div class="wrap">
<h2>Your Next Customer Is Out There.</h2>
<p>Give Ventoryn your website. Let intelligent agents discover
opportunities, prepare outreach, and help turn real engagement into
measurable customer growth.</p>
<p>Joining the waitlist is free registration, separate from the
$20/month subscription.</p>
<div class="hero-ctas">
<a class="btn btn-primary" href="/founding-customer-waitlist">Join the Founding Customer Waitlist</a>
<a class="btn btn-ghost" href="/account/login">Explore the Platform</a>
</div>
</div>
</section>
</main>
<footer class="site-footer"><div class="wrap">
<div class="cols">
<div class="col"><h4>Product</h4>
<a href="#product">Product</a>
<a href="#how-it-works">How It Works</a>
<a href="#attribution">Attribution</a>
<a href="#pricing">Pricing</a>
<a href="#faq">FAQ</a></div>
<div class="col"><h4>Account</h4>
<a href="/account/login">Account Login</a>
<a href="/account/subscribe">Subscribe — $20/month</a></div>
<div class="col"><h4>Company</h4>
<span class="soon">Documentation — coming soon</span>
<span class="soon">Support — coming soon</span>
<span class="soon">Privacy Policy — coming soon</span>
<span class="soon">Terms of Service — coming soon</span></div>
</div>
<p class="base">Ventoryn — AI customer acquisition for your website.</p>
</div></footer>
<script>
"use strict";
window.VENTORYN = {state_json};
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v == null ? "" : v)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
let jobId = null;
function setStatus(msg) {{ $("landing-status").textContent = msg; }}
function ctaFor(url) {{
  const box = $("landing-cta");
  const enc = encodeURIComponent(url);
  if (window.VENTORYN.subscribed) {{
    box.innerHTML = "<p><a class='btn btn-primary' href='/account/sites/new?url=" + enc + "'>Add this website to your account</a></p>";
  }} else if (window.VENTORYN.signedIn) {{
    box.innerHTML = "<p><a class='btn btn-primary' href='/account/subscribe?site_url=" + enc + "'>Subscribe — $20/month to add " + esc(url) + "</a></p>";
  }} else {{
    box.innerHTML = "<p><a class='btn btn-google' href='/account/auth/google/start?site_url=" + enc + "'>Sign in with Google to subscribe — $20/month</a></p>" +
      "<p class='muted'>Your website is carried through sign-in and checkout — you never retype it.</p>";
  }}
  box.hidden = false;
}}
function renderPreview(job) {{
  const r = job.result || {{}};
  const box = $("landing-preview");
  let html = "";
  if (r.fetchable === false) {{
    html += "<div class='score-row'>" +
      "<div class='score'><div class='label'>Site score</div><div class='value'>Unavailable</div></div>" +
      "<div class='score'><div class='label'>GEO score</div><div class='value'>Unavailable</div></div></div>" +
      "<p><strong>Could not fetch the site.</strong> " + esc(r.unavailable_reason || "Check the URL and try again.") + "</p>";
  }} else {{
    html += "<div class='score-row'>" +
      "<div class='score'><div class='label'>Site score</div><div class='value'>" + esc(r.site_score) + " / 100</div></div>" +
      "<div class='score'><div class='label'>GEO score</div><div class='value'>" + esc(r.geo_score) + " / 100</div></div></div>";
  }}
  const findings = (r.findings || []).slice(0, 5);
  if (findings.length) {{
    html += "<h3>Top findings — what Ventoryn would fix first</h3><ul class='findings'>";
    for (const f of findings) {{
      html += "<li><strong>[" + esc(f.severity) + " · " + esc(f.area) + "]</strong> " + esc(f.issue) + " — Fix: " + esc(f.fix) + "</li>";
    }}
    html += "</ul>";
  }} else if (r.fetchable !== false) {{
    html += "<p>No findings — the site is in good shape for this market.</p>";
  }}
  box.innerHTML = html;
  box.hidden = false;
  ctaFor(job.url);
}}
async function pollJob() {{
  if (!jobId) return;
  try {{
    const res = await fetch("/api/analyze/" + encodeURIComponent(jobId));
    const job = await res.json();
    if (job.status === "done") {{
      jobId = null;
      setStatus("Done — " + job.url);
      renderPreview(job);
      $("landing-submit").disabled = false;
    }} else if (job.status === "failed") {{
      jobId = null;
      setStatus("Could not analyze: " + (job.error || "the analysis failed."));
      $("landing-submit").disabled = false;
    }} else {{
      setStatus("Analyzing " + job.url + "… (this takes a few seconds)");
      setTimeout(pollJob, 3000);
    }}
  }} catch (err) {{
    setStatus("The preview could not load — please try again.");
    $("landing-submit").disabled = false;
  }}
}}
$("landing-analyze-form").addEventListener("submit", async (e) => {{
  e.preventDefault();
  let url = $("landing-url").value.trim();
  // A bare domain works — the server adds https://, and so do we,
  // so the URL shown and carried on is the canonical one.
  if (url && url.indexOf("://") === -1) url = "https://" + url;
  const country = $("landing-country").value.trim().toLowerCase();
  $("landing-preview").hidden = true;
  $("landing-preview").innerHTML = "";
  $("landing-cta").hidden = true;
  setStatus("Starting analysis…");
  $("landing-submit").disabled = true;
  const signin = $("landing-signin");
  if (signin) signin.href = "/account/auth/google/start?site_url=" + encodeURIComponent(url);
  try {{
    const res = await fetch("/api/analyze", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ url: url, country: country }}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
    jobId = data.job_id;
    setStatus("Analyzing " + data.url + "… (this takes a few seconds)");
    setTimeout(pollJob, 3000);
  }} catch (err) {{
    setStatus("Could not start the analysis: " + err.message);
    $("landing-submit").disabled = false;
  }}
}});
// Mobile public nav (§3): the toggle opens/closes the link list.
const navToggle = $("nav-toggle");
const navLinks = $("nav-links");
if (navToggle && navLinks) {{
  navToggle.addEventListener("click", () => {{
    const open = navLinks.classList.toggle("open");
    navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    navToggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  }});
  navLinks.addEventListener("click", (e) => {{
    if (e.target.tagName === "A") {{
      navLinks.classList.remove("open");
      navToggle.setAttribute("aria-expanded", "false");
    }}
  }});
}}
// Product dropdown (§3): toggle on tap/click; tap-outside closes.
const dropBtn = $("nav-drop-btn");
const dropItem = dropBtn && dropBtn.closest(".nav-item");
if (dropBtn && dropItem) {{
  dropBtn.addEventListener("click", function (e) {{
    e.stopPropagation();
    const open = dropItem.classList.toggle("open");
    dropBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }});
  document.addEventListener("click", function () {{
    dropItem.classList.remove("open");
    dropBtn.setAttribute("aria-expanded", "false");
  }});
  dropItem.querySelectorAll("a").forEach(function (a) {{
    a.addEventListener("click", function () {{
      dropItem.classList.remove("open");
      dropBtn.setAttribute("aria-expanded", "false");
    }});
  }});
}}
// Demo revenue reveal (§5): example figures only, hidden by default.
const demoToggle = $("demo-revenue-toggle");
const demoValue = $("demo-revenue-value");
if (demoToggle && demoValue) {{
  demoToggle.addEventListener("click", () => {{
    const hidden = demoValue.textContent.indexOf("hidden") !== -1;
    demoValue.textContent = hidden ? "$1,240 (example)" : "$••• hidden";
    demoToggle.textContent = hidden ? "Hide example revenue"
      : "Reveal example revenue";
  }});
}}
</script>
</body></html>"""

    def _own_snippet_landing() -> str:
        from ventoryn.analytics import own_snippet

        return own_snippet()

    @app.get("/", response_class=HTMLResponse)
    def landing_page(request: Request):
        """Public front door — no sign-in, no internal data."""
        return _landing_page(_current_account(request))

    # -- Websites: the account's own sites/businesses -----------------
    # Styled like Overview (stat cards + a styled list), not the bare
    # table it used to be. The operator sees his registry businesses
    # plus his own SaaS sites; a customer sees only their sites.
    # The page is "Websites" (renamed from "My Portfolio") and the
    # analyze box has exactly one home — Overview — so it does not
    # live here. Registry businesses stay as a clearly-labeled
    # second group, "Operator businesses".
    @app.get("/api/websites/revenue")
    def websites_revenue(request: Request):
        """The owner's revenue, on demand (the eye on Websites): the
        same totals the page shows, but including websites whose
        revenue display is off. Only the signed-in owner's own
        websites; never cached."""
        account = _current_account(request)
        if account is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        sites, site_profit = {}, {}
        total = costs = 0.0
        for site in _saas_store.list_sites(account["id"]):
            cost = _site_cost_usd(site["id"])
            costs += cost
            raw = _saas_store.site_revenue_total(site["id"])
            if raw is not None:
                rev = raw["total_cents"] / 100
                sites[site["id"]] = f"${rev:,.2f}"
                site_profit[site["id"]] = money(rev - cost)
                total += rev
        reg = [_biz_row(b) for b in _visible_businesses(account)]
        reg_rev = sum(r["revenue"] for r in reg if r["revenue"] is not None)
        reg_profit = sum(r["net_profit"] for r in reg if r["net_profit"] is not None)
        return JSONResponse({"sites": sites, "site_profit": site_profit,
                             "total": money(total + reg_rev),
                             "net_profit": money(total - costs + reg_profit)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio(request: Request):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        reg_rows = [_biz_row(b) for b in _visible_businesses(account)]
        site_rows = _site_business_rows(account)
        biz_rows = reg_rows + site_rows

        def _money(value):
            return "hidden" if value is None else money(value)
        any_hidden = any(r["revenue"] is None for r in biz_rows)
        total_revenue = sum(r["revenue"] for r in biz_rows
                            if r["revenue"] is not None)
        total_profit = sum(r["net_profit"] for r in biz_rows
                           if r["net_profit"] is not None)

        from ventoryn.saas_routes import _funnel_rows

        def _site_extra(r) -> str:
            """For the account's own websites: the funnel (visitors,
            signups, paying customers, revenue) that used to live on
            My account, plus the way into the site's page."""
            if not str(r["id"]).startswith("site:"):
                return ""
            site = _saas_store.get_site(str(r["id"])[5:])
            if site is None:
                return ""
            conn = _saas_store.get_connection(site["id"], "plausible") or {}
            key = ((conn.get("_detail_raw") or {}).get("api_key")
                   if conn.get("status") == "connected" else None)
            return (f"<div class='table-wrap v-biz-funnel'><table>"
                    f"{_funnel_rows(_saas_store, site, key)}</table></div>"
                    f"<p class='v-biz-open'><a href='{esc(site['url'])}' "
                    f"target='_blank' rel='noopener noreferrer'>"
                    f"Open website</a></p>")

        def _title(r) -> str:
            if str(r["id"]).startswith("site:"):
                return (f"<h3><a href='/account/sites/{esc(str(r['id'])[5:])}'>"
                        f"{esc(r['name'])}</a></h3>")
            return f"<h3>{esc(r['name'])}</h3>"

        def _cards(rows) -> str:
            return "".join(
                f"<article class='v-biz v-accent-{i % 4}'>"
                f"{_title(r)}"
                f"<p class='v-biz-url'>{esc(r['business_type'])} · "
                f"{esc(r['status'])}"
                + (" · <a class='pill' href='/businesses'>Unclaimed — claim"
                   "</a>" if r.get("owner_email", "x") is None else "")
                + f"</p>"
                f"<dl><dt>Revenue</dt><dd>{_money(r['revenue'])}</dd>"
                f"<dt>Costs</dt><dd>{money(r['total_costs'])}</dd>"
                f"<dt>Net profit</dt><dd>{_money(r['net_profit'])}</dd></dl>"
                f"{_site_extra(r)}"
                f"</article>"
                for i, r in enumerate(rows)) or (
                "<div class='card'><p>No websites yet. "
                "<a href='/account/sites/new'>Add your first website</a> — "
                "Ventoryn works to bring it new paying "
                "customers.</p></div>")
        def _site_list(rows) -> str:
            """Your websites as a compact list — one row per website,
            the key numbers only; the full funnel lives on each
            website's own page. Scrolls, with a filter past 6 rows."""
            if not rows:
                return _cards(rows)
            from ventoryn.analytics import analytics_for

            va = analytics_for(_saas_store)
            dash = "<span class='muted'>—</span>"
            body = ""
            for r in rows:
                site = _saas_store.get_site(str(r["id"])[5:])
                if site is None:
                    continue
                sid = esc(site["id"])
                summary = va.summary(site["id"])
                visitors = (f"{summary['visitors']:,}" if summary["installed"]
                            else f"<a class='muted' href='/account/sites/{sid}#ventoryn-analytics'>Set up</a>")
                rev = _saas_store.revenue_for_site(site["id"])
                stripe_on = (_saas_store.get_connection(site["id"], "stripe_sales")
                             or {}).get("status") == "connected"
                if rev is None:
                    paying, revenue = dash, dash
                else:
                    paying = ("hidden" if rev["paying_customers"] is None
                              else f"{rev['paying_customers']:,}")
                    if not stripe_on:
                        revenue = (f"<a class='muted' href='/integrations/stripe_sales"
                                   f"?site={sid}'>Connect Stripe</a>")
                    elif rev["revenue"] is None:
                        revenue = f"<span data-rev-site='{sid}'>hidden</span>"
                    else:
                        revenue = (f"${rev['revenue']['total_cents'] / 100:,.2f}")
                from ventoryn.nav import autopilot_switch_html

                auto = autopilot_switch_html(
                    site["id"], bool(int(site.get("autopilot", 1) or 0)),
                    r["name"], return_to="/portfolio")
                body += (f"<tr><td><a class='v-site-name' href='/account/sites/{sid}'>"
                         f"{esc(r['name'])}</a></td>"
                         f"<td class='num'>{visitors}</td><td class='num'>{paying}</td>"
                         f"<td class='num'>{revenue}</td><td>{auto}</td>"
                         f"<td><a href='{esc(site['url'])}' target='_blank' "
                         f"rel='noopener noreferrer'>Open</a></td></tr>")
            n = len(rows)
            tools = ""
            if n > 6:
                tools = (f"<div class='v-site-tools'><label class='sr-only' "
                         f"for='site-filter'>Filter websites</label>"
                         f"<input id='site-filter' type='search' "
                         f"placeholder='Filter {n} websites' autocomplete='off'>"
                         f"<span class='muted' id='site-count'>{n} websites</span></div>"
                         "<script>(function(){var f=document.getElementById('site-filter');"
                         "var c=document.getElementById('site-count');"
                         "f.addEventListener('input',function(){var q=f.value.trim().toLowerCase(),k=0;"
                         "document.querySelectorAll('#site-list tbody tr').forEach(function(el){"
                         "var hit=!q||el.textContent.toLowerCase().indexOf(q)>=0;el.hidden=!hit;if(hit)k++;});"
                         "c.textContent=k+(k===1?' website':' websites');});})();</script>")
            return (f"{tools}<div class='v-site-list' id='site-list' tabindex='0' "
                    f"role='region' aria-label='Your websites'><table class='v-site-table'>"
                    f"<thead><tr><th>Website</th><th class='num'>Visitors (30 days)</th>"
                    f"<th class='num'>Paying customers</th><th class='num'>Revenue</th>"
                    f"<th>Autopilot</th><th></th></tr></thead><tbody>{body}</tbody>"
                    f"</table></div>")

        hidden_note = ("<p class='muted'>Revenue from a site whose "
                       "revenue display is off is not included in these "
                       "totals — the site shows &quot;hidden&quot; instead.</p>"
                       if any_hidden else "")
        # Registry businesses are a second, clearly-labeled group —
        # never mixed into the customer's own websites.
        # Recent jobs (moved here from My account): which website each
        # job ran on, what it was, and what it cost.
        from ventoryn.saas import job_cost_label as _job_cost

        _is_op = _saas_config.is_operator(account["email"])
        hosts = {s["id"]: s["host"] for s in _saas_store.list_sites(account["id"])}
        job_rows = "".join(
            f"<tr><td>{esc(hosts.get(j['site_id'], '—'))}</td>"
            f"<td>{esc(j['job_type'].replace('_', ' '))}</td>"
            f"<td>{esc(j['status'])}</td>"
            f"<td>{esc(_job_cost(j, _is_op))}</td>"
            f"<td>{esc(j['result'] or '—')}</td></tr>"
            for j in _saas_store.list_jobs(account["id"])
        ) or "<tr><td colspan='5'>No jobs yet.</td></tr>"
        reg_group = ""
        if reg_rows:
            reg_group = (f"<h2>Operator businesses</h2>"
                         f"<p class='muted'>Registry businesses managed "
                         f"from this account. "
                         f"<a href='/businesses'>View all businesses</a></p>"
                         f"<div class='v-biz-grid'>{_cards(reg_rows)}</div>")
        _first = ("<div class='v-stat v-stat-1'><span class='v-stat-label'>Websites</span>"
                  f"<span class='v-stat-value'>{len(biz_rows)}</span></div>")
        inner = f"""{_money_stats_html(account, first=_first)}
<p class='muted'>Costs are the credits each website's agent work used,
valued at the plan rate ({money(_credit_usd(), 2)} a credit). Revenue is
real — the sum of recorded paying customers per site.</p>{hidden_note}
<p><a class='btn' href='/account/sites/new'>Add a website</a></p>
<h2>Your websites</h2>
{_site_list(site_rows)}
<h2>Recent jobs</h2>
<div class='table-wrap'><table>
<tr><th>Website</th><th>Job</th><th>Status</th><th>Cost</th><th>Result</th></tr>
{job_rows}</table></div>
{reg_group}"""
        return _operator_page(
            "Websites", inner, account=account, current="portfolio",
            is_operator=_saas_config.is_operator(account["email"]),
            extra_css=_PORTFOLIO_CSS)

    @app.get("/businesses", response_class=HTMLResponse)
    def business_list(request: Request):
        account, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        led: Ledger = state["ledger"]
        businesses = _visible_businesses(account)

        def _owner_cell(b) -> str:
            if b.owner_email:
                return "<span class='pill green'>Yours</span>"
            return (f"<form method='post' action='/businesses/"
                    f"{esc(b.id)}/claim' style='margin:0'>"
                    f"<button type='submit' class='primary'>Claim</button>"
                    f"</form>")
        rows = "".join(
            f"<tr><td><a href='/businesses/{esc(b.id)}'>{esc(b.name)}</a></td>"
            f"<td>{esc(b.business_type)}</td>"
            f"<td><span class='pill'>{esc(b.status.value)}</span></td>"
            f"<td class='num'>{money(led.pnl(b.id).net_profit)}</td>"
            f"<td>{_owner_cell(b)}</td></tr>"
            for b in businesses
        ) or "<tr><td colspan='5' class='muted'>No businesses yet.</td></tr>"
        unclaimed = sum(1 for b in businesses if not b.owner_email)
        note = (f"<div class='v-notice v-notice-warn'>{unclaimed} "
                f"unclaimed — claim them to manage them from your "
                f"account ({esc(account['email'])}). Only the owning "
                f"account sees a business, its approvals and its "
                f"data.</div>" if unclaimed else "")
        inner = f"""{note}<div class="table-wrap"><table>
<tr><th>Name</th><th>Type</th><th>Status</th><th class="num">Net profit</th><th>Owner</th></tr>{rows}</table></div>
<a class="v-back" href="/portfolio">{icon('arrow_left', 15)} Websites</a>"""
        return _operator_page(f"Businesses ({len(businesses)})", inner,
                              account=account, current="businesses",
                              is_operator=True)

    @app.post("/businesses/{business_id}/claim")
    def business_claim(request: Request, business_id: str):
        """Bind an unclaimed business to the signed-in operator's own
        account. A business that already has an owner cannot be
        claimed (or re-claimed) here."""
        account, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        biz = state["registry"].get(business_id)
        if biz is None or biz.owner_email:
            raise HTTPException(404, "business not found")
        state["registry"].assign_owner(business_id, account["email"])
        state["audit"].record(
            agent_type="owner", event="business_claimed",
            business_id=business_id,
            result={"owner_email": account["email"].strip().lower()})
        return RedirectResponse("/businesses", status_code=303)

    @app.get("/businesses/{business_id}", response_class=HTMLResponse)
    def business_detail(request: Request, business_id: str):
        account, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        reg: BusinessRegistry = state["registry"]
        led: Ledger = state["ledger"]
        aud: AuditLog = state["audit"]
        biz = reg.get(business_id)
        if biz is None or business_id not in _owned_business_ids(account):
            raise HTTPException(404, "business not found")
        pnl = led.pnl(business_id)
        events = aud.for_business(business_id)
        net_cls = "pos" if pnl.net_profit >= 0 else "neg"
        inner = f"""<p class="muted">{esc(biz.business_type)} ·
<span class="pill">{esc(biz.status.value)}</span></p>
<div class="v-kpis">
<div class="v-kpi"><span class="label">Revenue</span><span class="value">{money(pnl.revenue)}</span></div>
<div class="v-kpi"><span class="label">Costs</span><span class="value">{money(pnl.total_costs)}</span></div>
<div class="v-kpi"><span class="label">Net profit</span><span class="value {net_cls}">{money(pnl.net_profit)}</span></div>
<div class="v-kpi"><span class="label">Net margin</span><span class="value">{pnl.net_margin:.1%}</span></div>
</div>
<p class="muted">P&amp;L · {len(events)} audit events</p>
<a class="v-back" href="/businesses">{icon('arrow_left', 15)} All businesses</a>"""
        return _operator_page(biz.name, inner, account=account,
                              current="businesses", is_operator=True)

    @app.get("/experiments", response_class=HTMLResponse)
    def experiment_list(request: Request):
        account, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        exp: ExperimentEngine = state["experiments"]
        rows = "".join(
            f"<tr><td><code>{esc(e.id)}</code></td><td>{esc(e.business_id)}</td>"
            f"<td><span class='pill'>{esc(e.status.value)}</span></td>"
            f"<td>{esc(e.recommendation or '—')}</td></tr>"
            for e in exp._experiments.values()
        ) or "<tr><td colspan='4' class='muted'>No experiments yet.</td></tr>"
        inner = f"""<div class="table-wrap"><table>
<tr><th>ID</th><th>Business</th><th>Status</th><th>Recommendation</th></tr>
{rows}</table></div><a class="v-back" href="/ops">{icon('arrow_left', 15)} Overview</a>"""
        return _operator_page("Experiments", inner, account=account,
                              current=None, is_operator=True)

    def _decide_banner_html(gate, decided: str) -> str:
        """The confirmation after a decide: "Approved — nothing
        ships without your say-so." Shared by /approvals and the
        Overview decision strip — both post to the same decide
        endpoint and redirect back with ?decided=<id>."""
        if not decided:
            return ""
        item = gate.get(decided)
        if item is not None and item.status.value in ("approved",
                                                      "rejected"):
            verb = ("Approved" if item.status.value == "approved"
                    else "Rejected")
            kind = ("ok" if item.status.value == "approved" else "bad")
            return (f"<div class='v-notice v-notice-{kind}' role='status'>"
                    f"{verb} — nothing ships without your say-so.</div>")
        return ""

    def _draft_cost_text(draft) -> str:
        """The cost line for a social draft, shown before Approve.
        X shows the dollar estimate with its honesty note; Facebook
        and Instagram show the note alone — the note itself says not
        to quote $0, so no $0.00 figure is ever rendered for them."""
        provider = draft.get("kind") or ""
        note = rate_note(provider)
        if provider == "x":
            est = estimate_cost(provider, bool(draft.get("link_url")))
            return f"${est:.2f} — {note}"
        return note

    @app.get("/approvals", response_class=HTMLResponse)
    def approval_queue(request: Request, decided: str = ""):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        gate: ApprovalGate = state["approvals"]
        reg: BusinessRegistry = state["registry"]
        # Only the approvals for businesses this account owns. The
        # operator owns the registry businesses — and sees unassigned
        # approvals (business_id None) too, as operator oversight. A
        # customer sees only their own — an approval for someone
        # else's business is never shown, and never decidable, here.
        owned = _owned_business_ids(account)

        def _sees(a) -> bool:
            return _approval_visible(account, a, owned)
        pending = [a for a in gate.pending() if _sees(a)]
        decided_list = [a for a in gate.decided(20) if _sees(a)]
        # Social drafts are the same approval story on a different
        # machine: a post the account must approve before it can
        # publish. Pending ones sit beside the internal approvals;
        # decided ones beside the decided list. Only this account's
        # own sites' drafts — the site lookup enforces ownership.
        # The decide forms post to the draft approve/reject
        # endpoints (same ownership check as the site page) and
        # carry the two-factor field when 2FA is enabled.
        totp_field = ""
        if account.get("totp_enabled"):
            totp_field = (
                "<label>Two-factor code (fresh — required to confirm "
                "this change)</label><input type='text' "
                "name='totp_code' inputmode='numeric' "
                "autocomplete='one-time-code' maxlength='8'>")
        draft_pend_rows = ""
        draft_dec_rows = ""
        draft_pending_count = 0
        for _site in _saas_store.list_sites(account["id"]):
            for _draft in _saas_store.list_social_drafts(_site["id"]):
                _provider = (_draft.get("kind") or "").capitalize()
                _base = (f"/account/sites/{esc(_site['id'])}/social/"
                         f"{esc(_draft.get('kind') or '')}/drafts/"
                         f"{esc(_draft['id'])}")
                if _draft.get("status") == "pending":
                    draft_pending_count += 1
                    draft_pend_rows += (
                        f"<tr><td><strong>{esc(_provider)} post</strong> — "
                        f"{esc(_draft.get('text') or '')}<br>"
                        f"<span class='muted'>Cost if published: "
                        f"<strong>{esc(_draft_cost_text(_draft))}</strong>"
                        f"</span></td>"
                        f"<td><form method='post' action='{_base}/approve'>"
                        f"{totp_field}<p>"
                        "<button class='btn btn-primary' type='submit'>"
                        "Approve &amp; post</button> "
                        "<button class='btn' type='submit' "
                        f"formaction='{_base}/reject'>Reject</button>"
                        "</p></form></td></tr>")
                elif _draft.get("status") in ("posted", "rejected"):
                    _when = (str(_draft.get("decided_at") or "")
                             .replace("T", " ")[:19] or "—")
                    _post = _draft.get("tweet_id") or ""
                    draft_dec_rows += (
                        f"<tr><td><strong>{esc(_provider)} post</strong> — "
                        f"{esc(_draft.get('text') or '')}"
                        + (f"<br><span class='muted'>Posted as "
                           f"{esc(_post)}</span>" if _post else "")
                        + "</td>"
                        f"<td>{esc(_draft['status'].capitalize())}</td>"
                        f"<td>{esc(_when)}</td>"
                        "<td>—</td></tr>")
        # Pending rows are expandable: the human-readable summary is
        # always visible, and the full proposed action (raw payload)
        # is one click away — visible before Approve. The decide
        # endpoint is unchanged; the row only shows more.
        pend_rows = "".join(
            f"<tr><td>{_approval_summary(a, reg)}"
            f"<details class='v-expandable'><summary>Full payload</summary>"
            f"<pre class='v-approval-payload'>"
            f"{esc(json.dumps(a.details or {}, indent=2, default=str))}"
            f"</pre></details></td>"
            f"<td><form method='post' action='/approvals/{esc(a.id)}/decide'>"
            f"<button name='approved' value='true'>Approve</button>"
            f"<button name='approved' value='false'>Reject</button></form></td></tr>"
            for a in pending
        )
        pend_rows = (pend_rows + draft_pend_rows) or \
            "<tr><td colspan='2'>Nothing awaiting approval.</td></tr>"

        def _decided_when(a) -> str:
            if getattr(a, "decided_at", None):
                return esc(str(a.decided_at).replace("T", " ")[:19])
            return "—"
        dec_rows = "".join(
            f"<tr><td>{_approval_summary(a, reg)}</td>"
            f"<td>{esc(a.status.value)}</td>"
            f"<td>{_decided_when(a)}</td>"
            f"<td>{esc(a.decided_by or '—')}</td></tr>"
            for a in decided_list
        )
        dec_rows = (dec_rows + draft_dec_rows) or \
            "<tr><td colspan='4'>No decisions yet.</td></tr>"
        # Confirmation after a decide: the POST redirects back with
        # ?decided=<id>, and the decided item is already out of
        # pending and in the Decided section below. Nothing may still
        # look pending after it was decided.
        banner = _decide_banner_html(gate, decided)
        inner = f"""<div class="v-approvals">{banner}{_approvals_campaigns_html(account)}<div class="card"><p class="muted">Nothing is published or executed without your sign-off. Approving a mission report records your approval of its drafts &mdash; nothing is published automatically. Approving a <strong>supplier</strong> publishes that product to Shopify on the next publish run.</p></div>
<div class="card"><h2>Pending ({len(pending) + draft_pending_count})</h2>
<table>
<tr><th>Request</th><th>Decide</th></tr>{pend_rows}</table></div>
<div class="card"><h2>Decided</h2>
<table>
<tr><th>Request</th><th>Decision</th><th>Decided</th><th>By</th></tr>{dec_rows}</table></div></div>"""
        return _operator_page(
            "Approvals", inner, account=account, current="approvals",
            is_operator=_saas_config.is_operator(account["email"]),
            extra_css=_APPROVALS_CSS)

    @app.post("/approvals/{approval_id}/decide")
    def decide_approval(request: Request, approval_id: str,
                        approved: str = Form(...),
                        next_path: str = Form("", alias="next")):
        """The ONE decide endpoint — the /approvals rows and the
        Overview decision strip both post here, with the same
        ownership check and the same gate.decide call. ``next`` is a
        same-site return path (the Overview strip sends /ops); any
        other value falls back to /approvals, so an external URL can
        never be injected as a redirect."""
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        gate: ApprovalGate = state["approvals"]
        # Ownership is enforced, not just hidden: an approval for a
        # business the signer does not own cannot be decided by them.
        # Nothing ships until the owning customer approves it.
        try:
            target = next(a for a in
                          gate.pending() + gate.decided(50)
                          if a.id == approval_id)
        except StopIteration:
            target = None
        if target is not None and not _approval_visible(account, target):
            return HTMLResponse(
                _operator_page(
                    "Approvals",
                    "<p>This approval is not yours to decide.</p>"
                    "<p><a href='/approvals'>Back</a></p>",
                    account=account, current="approvals",
                    is_operator=_saas_config.is_operator(account["email"])),
                status_code=403)
        try:
            gate.decide(approval_id, approved=approved.lower() == "true",
                        decided_by=account["email"])
        except ValueError as exc:
            return HTMLResponse(
                _operator_page(
                    "Approvals",
                    f"<p>Could not record decision: {esc(exc)}</p>"
                    "<p><a href='/approvals'>Back</a></p>",
                    account=account, current="approvals",
                    is_operator=_saas_config.is_operator(account["email"])),
                status_code=400,
            )
        # Redirect back — to the page that sent the decision (the
        # Overview strip's /ops, or /approvals by default) — the item
        # is out of pending and under Decided with a confirmation,
        # never still pending.
        dest = (next_path if (next_path.startswith("/")
                             and not next_path.startswith("//"))
                else "/approvals")
        return RedirectResponse(f"{dest}?decided={approval_id}",
                                status_code=303)

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request):
        """Mission reports (e.g. marketing missions), newest first."""
        account, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        from ventoryn.reports import list_reports

        items = list_reports()
        rows = "".join(
            f"<tr><td><a href='/reports/{esc(r['name'])}'>{esc(r['title'])}</a></td>"
            f"<td>{esc(r['mission'])}</td>"
            f"<td>{esc(r['date'] or r['generated_at'] or '—')}</td>"
            f"<td class='num'>{('$%.4f' % r['spend_usd']) if r['spend_usd'] is not None else '—'}</td></tr>"
            for r in items
        ) or ("<tr><td colspan='4'>No mission reports yet. Run a mission "
                "(e.g. <code>python3 ecosystem/marketing_mission.py --llm</code>) "
                "to produce one.</td></tr>")
        inner = f"""<p class="muted">Agent mission outputs, newest first. Reports are drafts —
nothing in them was published.</p>
<div class="table-wrap"><table>
<tr><th>Report</th><th>Mission</th><th>Date</th><th class="num">AI spend</th></tr>
{rows}</table></div>
<a class="v-back" href="/approvals">Go to Approvals {icon('arrow_right', 15)}</a>"""
        return _operator_page("Mission reports", inner, account=account,
                              current="reports", is_operator=True)

    @app.get("/reports/{name}", response_class=HTMLResponse)
    def report_view(request: Request, name: str):
        """Render one mission report as safe HTML."""
        account, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        from ventoryn.reports import render_report

        result = render_report(name)
        if result is None:
            raise HTTPException(404, "report not found")
        title, body = result
        return _operator_page(
            title,
            f"""<div class="v-doc-wrap"><article class="card v-doc">{body}</article>
<a class="v-back" href="/reports">{icon('arrow_left', 15)} Back to reports</a></div>""",
            account=account, current="reports", is_operator=True,
            extra_css=ui.DOC_CSS)

    @app.get("/reports/{name}/download", response_class=FileResponse)
    def report_download(request: Request, name: str):
        """Download one mission report as Markdown. Operator only —
        the same validated path as render_report, so path
        traversal is impossible."""
        account, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        from ventoryn.reports import report_path

        path = report_path(name)
        if path is None:
            raise HTTPException(404, "report not found")
        return FileResponse(path, media_type="text/markdown",
                            filename=name)

    @app.get("/experiment-001", response_class=HTMLResponse)
    def experiment_001(request: Request):
        """Live Experiment 001 (Evergreen Planners / Etsy) status."""
        account, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        try:
            from businesses.etsy_evergreen.monitor import (
                EtsyCredentialsError,
                check_experiment_001,
            )

            snapshot = check_experiment_001()
        except EtsyCredentialsError as exc:
            return _operator_page(
                "Experiment 001",
                f"<div class='v-notice v-notice-warn'>Etsy is not "
                f"configured on this server.</div>"
                f"<p class='muted'>{esc(exc)}</p>"
                f"<a class='v-back' href='/ops'>{icon('arrow_left', 15)} Overview</a>",
                account=account, current=None, is_operator=True)
        except Exception as exc:  # noqa: BLE001 - dashboard must stay up
            return _operator_page(
                "Experiment 001",
                f"<div class='v-notice v-notice-bad'>Monitor error: "
                f"{esc(exc)}</div>"
                f"<a class='v-back' href='/ops'>{icon('arrow_left', 15)} Overview</a>",
                account=account, current=None, is_operator=True)
        triggers = "".join(
            f"<li>{esc(t)}</li>" for t in snapshot["stop_loss_triggers"]
        ) or "<li>none</li>"
        kind = {"ON_TRACK": "ok", "BEHIND": "warn",
                "STOP_LOSS": "bad"}[snapshot["verdict"]]
        inner = f"""<p class="muted">Checked {esc(snapshot["checked_at"])} ·
Day {snapshot["days_elapsed"]}/60 ({snapshot["days_remaining"]} remaining)</p>
<div class="v-notice v-notice-{kind}">{snapshot["verdict"]}</div>
<div class="table-wrap"><table>
<tr><th>Metric</th><th>Actual</th><th>Target</th></tr>
<tr><td>Revenue</td><td>${snapshot["revenue_usd"]:,.2f}</td>
    <td>${snapshot["target_revenue_usd"]:,.2f}</td></tr>
<tr><td>Orders</td><td>{snapshot["orders"]}</td>
    <td>{snapshot["target_orders"]}</td></tr>
<tr><td>Active listings</td><td>{snapshot["active_listings"]}</td><td>12</td></tr>
<tr><td>Spend</td><td>${snapshot["spend_usd"]:,.2f}</td><td>$91.00</td></tr>
<tr><td>Contribution margin</td><td>${snapshot["contribution_margin_usd"]:,.2f}</td><td>&gt; $0</td></tr>
<tr><td>Pace vs target</td><td>{snapshot["pace_pct"]}%</td><td>100%</td></tr>
</table></div>
<h2>Stop-loss triggers</h2>
<ul>{triggers}</ul>
<h2>Weekly revenue (last 3 weeks)</h2>
<p>{", ".join(money(w) for w in snapshot["weekly_revenue_usd"])}</p>
<a class="v-back" href="/ops">{icon('arrow_left', 15)} Overview</a>"""
        return _operator_page("Experiment 001 — Evergreen Planners", inner,
                              account=account, current=None,
                              is_operator=True)

    # -- Live Ops API (powers the /ops live view; /game is the old address) ---
    # All state transitions go through the real runtime components; these
    # endpoints only serialize what the orchestrator/ledger/audit produced.
    @app.get("/game", response_class=HTMLResponse)
    def game_redirect(request: Request):
        """The old /game address redirects to the professional
        Overview — the game view is gone."""
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        return RedirectResponse("/ops", status_code=303)

    def _decision_strip_html(pending, reg) -> str:
        """The "Needs your decision" strip on Overview: the top
        pending approval, inline, with Approve/Reject. The buttons
        post to the SAME /approvals/{id}/decide endpoint as the
        Approvals page (same ownership check, same gate.decide),
        with next=/ops so the confirmation lands back here. The
        full proposed action is visible before Approve — the
        human-readable summary plus the raw payload one click away.
        When nothing is pending, a quiet line instead of an empty
        card."""
        if not pending:
            return ("<div class='card'><h2>Needs your decision</h2>"
                    "<p class='muted'>Nothing awaiting approval. The "
                    "agents will bring the next draft here.</p></div>")
        a = pending[0]
        more = ""
        if len(pending) > 1:
            more = (f"<p><a href='/approvals'>View all {len(pending)} "
                    f"pending approvals</a></p>")
        else:
            more = "<p><a href='/approvals'>View on Approvals</a></p>"
        return (f"<div class='card'><h2>Needs your decision</h2>"
                f"{_approval_summary(a, reg)}"
                f"<details class='v-expandable'><summary>Full payload</summary>"
                f"<pre class='v-approval-payload'>"
                f"{esc(json.dumps(a.details or {}, indent=2, default=str))}"
                f"</pre></details>"
                f"<form method='post' action='/approvals/{esc(a.id)}/decide'>"
                f"<input type='hidden' name='next' value='/ops'>"
                f"<button class='btn btn-primary' name='approved' "
                f"value='true'>Approve</button> "
                f"<button class='btn' name='approved' value='false'>"
                f"Reject</button></form>"
                f"{more}</div>")

    def _draft_strip_html(draft, account) -> str:
        """The "Needs your decision" strip for a social draft: the
        post text, the cost if published (shown before Approve), and
        Approve/Reject posting to the draft approve/reject endpoints
        (same ownership check as the site page, two-factor field
        when 2FA is enabled)."""
        provider = draft.get("kind") or ""
        base = (f"/account/sites/{esc(draft.get('site_id') or '')}"
                f"/social/{esc(provider)}/drafts/"
                f"{esc(draft.get('id') or '')}")
        totp_field = ""
        if account.get("totp_enabled"):
            totp_field = (
                "<label>Two-factor code (fresh — required to confirm "
                "this change)</label><input type='text' "
                "name='totp_code' inputmode='numeric' "
                "autocomplete='one-time-code' maxlength='8'>")
        return (f"<div class='card'><h2>Needs your decision</h2>"
                f"<p><strong>{esc(provider.capitalize())} post</strong> — "
                f"{esc(draft.get('text') or '')}</p>"
                f"<p class='muted'>Cost if published: "
                f"<strong>{esc(_draft_cost_text(draft))}</strong></p>"
                f"<form method='post' action='{base}/approve'>"
                f"{totp_field}<p>"
                "<button class='btn btn-primary' type='submit'>"
                "Approve &amp; post</button> "
                "<button class='btn' type='submit' "
                f"formaction='{base}/reject'>Reject</button>"
                "</p></form>"
                "<p><a href='/approvals'>View on Approvals</a></p></div>")

    def _agent_costs_card_html(account) -> str:
        """What the agents' work is costing — 24 h / 7 days / 30 days.
        The owner sees the real AI spend (USD + tokens, from the audit
        log) and the credit value of the jobs he ran (he is not charged,
        so the jobs' price is what the work is worth). A customer sees
        the credits their jobs used."""
        from datetime import datetime, timedelta, timezone
        from ventoryn.saas import CREDIT_COSTS

        now = datetime.now(timezone.utc)
        is_op = _saas_config.is_operator(account["email"])
        jobs = _saas_store.jobs_since(account["id"],
                                      (now - timedelta(days=30)).isoformat())
        ai = agent_cost_windows(state["audit"], now) if is_op else None
        cells = ""
        for days, label in ((1, "Last 24 hours"), (7, "Last 7 days"), (30, "Last 30 days")):
            since = (now - timedelta(days=days)).isoformat()
            window = [j for j in jobs if j["created_at"] >= since]
            credits = sum((j["credits_charged"] or
                           (CREDIT_COSTS.get(j["job_type"], 0) if is_op else 0))
                          for j in window)
            lines = ""
            if ai is not None:
                w = ai[days]
                lines += (f"<span class='v-cost-big'>${w['cost']:,.4f}</span>"
                          f"<span class='v-cost-sub'>AI spend — {w['tokens']:,} tokens"
                          f" — {w['runs']} run{'s' if w['runs'] != 1 else ''}</span>")
            else:
                lines += f"<span class='v-cost-big'>{credits:,} credits</span>"
            lines += (f"<span class='v-cost-sub'>{len(window)} job"
                      f"{'s' if len(window) != 1 else ''} — {credits:,} credits"
                      + (" (not charged)" if is_op and credits else "") + "</span>")
            cells += f"<div class='v-cost-cell'><span class='v-cost-label'>{label}</span>{lines}</div>"
        return (f"<div class='card v-costs'><h2>Agent costs</h2>"
                f"<div class='v-cost-grid'>{cells}</div>"
                f"<p class='muted' style='margin:10px 0 0'>"
                + ("Real AI spend from every agent run, counted once per run. "
                   if is_op else "Credits used by your agents' jobs. ")
                + "<a href='/agents'>Cost per agent</a></p></div>")

    @app.get("/ops", response_class=HTMLResponse)
    def live_ops_page(request: Request, decided: str = ""):
        """The professional Overview: the top summary (progress ring
        + verified/pending stats), the "Needs your decision" strip,
        the websites, the analyze-the-site box, the merged Results,
        and the Live activity feed — all server-rendered through the
        platform shell, all from real store data. The full Pause
        card is gone from here (a slim pill in the header row is the
        operator's pause control); the analyze box has exactly one
        home here. No game design: no crew, no HUD, no runtime
        controls, no Experiment 001, no tasks/spend bars here
        (those live on the Agents page). Sign-in required — /ops
        is the post-sign-in home for every account."""
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        is_op = _saas_config.is_operator(account["email"])
        snap = _snapshot(account)
        data = _attribution_data(account, check_visitors=False,
                                 return_to="/ops")
        counts = data["counts"]

        def _money(value):
            return "—" if value is None else money(value)

        def _rev_cell(b: dict, value, kind: str = "site") -> str:
            """A website's revenue / net profit; when hidden, marked so
            the header Revenue switch can reveal it (net profit equals
            revenue for a website: its costs are credits, not dollars)."""
            if value is None and str(b["id"]).startswith("site:"):
                return (f"<span data-rev-{kind}='{esc(str(b['id'])[5:])}'>"
                        f"—</span>")
            return _money(value)

        def _biz_extra(b: dict) -> str:
            """One line per website card: verified customers for
            that site + a View link to the site page. Registry
            businesses get a View link to the business page
            instead — they have no SaaS verified count."""
            if str(b["id"]).startswith("site:"):
                site_id = str(b["id"])[5:]
                site = _saas_store.get_site(site_id)
                if site is not None and site.get("count_show", 1):
                    v = _saas_store.conversion_status_counts(
                        [site_id])["verified"]
                    line = (f"{v} verified customer"
                            f"{'s' if v != 1 else ''}")
                else:
                    line = "Verified customers hidden"
                return (f"<p class='v-biz-extra'>{esc(line)} — "
                        f"<a href='/account/sites/{esc(site_id)}'>"
                        f"View</a></p>")
            return (f"<p class='v-biz-extra'><a href='/businesses/"
                    f"{esc(b['id'])}'>View</a></p>")

        biz_cards = "".join(
            f"<article class='v-biz v-accent-{i % 4}'>"
            f"<h3>{esc(b['name'])}</h3>"
            f"<p class='v-biz-url'>{esc(b['business_type'])} — "
            f"{esc(b['status'])}"
            + (" — <a class='pill' href='/businesses'>Unclaimed — claim"
               "</a>" if b.get("owner_email", "x") is None else "")
            + f"</p>"
            f"<dl><dt>Revenue</dt><dd>{_rev_cell(b, b['revenue'])}</dd>"
            f"<dt>Costs</dt><dd>{money(b['total_costs'])}</dd>"
            f"<dt>Net profit</dt><dd>{_rev_cell(b, b['net_profit'], 'site-profit')}</dd></dl>"
            f"{_biz_extra(b)}"
            f"</article>"
            for i, b in enumerate(snap["businesses"])) or (
            "<div class='card'><p>No websites yet. "
            "<a href='/account/sites/new'>Add your first website</a> — "
            "Ventoryn works to bring it new paying "
            "customers.</p></div>")
        # The landing funnel: a visitor who typed a URL on the
        # landing page arrives here with ?site_url= — prefill the
        # analyze box with it and, for a customer who has not
        # subscribed yet, offer the subscribe step for that URL so
        # it is never retyped. Only a real http(s) URL is accepted.
        site_url = (request.query_params.get("site_url") or "").strip()
        prefill = site_url if site_url.startswith(
            ("http://", "https://")) else ""
        show_prompt = bool(prefill) and not is_op and \
            account["subscription_status"] != "active"
        # The decision strip: the top pending approval the viewer
        # may decide (same visibility rule as /approvals). When no
        # internal approval is pending, the top pending social draft
        # takes the strip instead — same Approve/Reject, same cost
        # shown before the decision.
        gate = state["approvals"]
        owned = _owned_business_ids(account)
        pending_here = [a for a in gate.pending()
                        if _approval_visible(account, a, owned)]
        if pending_here:
            decision_html = _decision_strip_html(pending_here,
                                                 state["registry"])
        else:
            draft_pending = []
            for s in _saas_store.list_sites(account["id"]):
                draft_pending.extend(
                    [d for d in _saas_store.list_social_drafts(s["id"])
                     if d.get("status") == "pending"])
            decision_html = (
                _draft_strip_html(draft_pending[0], account)
                if draft_pending
                else _decision_strip_html([], state["registry"]))
        banner = _decide_banner_html(gate, decided)
        # Data & privacy controls are a single quiet link here,
        # opening the site page — the full controls live there.
        footer = ""
        if data["sites"]:
            footer = (f"<p class='muted'><a href='/account/sites/"
                      f"{esc(data['sites'][0]['id'])}'>Data &amp; privacy "
                      f"controls</a></p>")
        if snap["businesses"]:
            # A swipeable carousel, not a grid: one card per website,
            # scroll-snap does the work on touch, the arrow buttons
            # are the non-touch fallback.
            websites_section = (
                "<h2>Your websites</h2>"
                "<div class='v-carousel-wrap'>"
                "<button class='v-carousel-btn v-carousel-prev' type='button' "
                "aria-label='Previous website'>"
                "<svg width='16' height='16' viewBox='0 0 16 16' aria-hidden='true'>"
                "<path d='M10 2L4 8l6 6' fill='none' stroke='currentColor' "
                "stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/>"
                "</svg></button>"
                f"<div class='v-carousel' role='list'>{biz_cards}</div>"
                "<button class='v-carousel-btn v-carousel-next' type='button' "
                "aria-label='Next website'>"
                "<svg width='16' height='16' viewBox='0 0 16 16' aria-hidden='true'>"
                "<path d='M6 2l6 6-6 6' fill='none' stroke='currentColor' "
                "stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/>"
                "</svg></button>"
                "</div>"
                "<script>(function(){"
                "document.querySelectorAll('.v-carousel-wrap').forEach(function(wrap){"
                "var track=wrap.querySelector('.v-carousel');"
                "function step(){var first=track.children[0];if(!first)return 300;"
                "var gap=parseFloat(getComputedStyle(track).gap||'14');"
                "return first.offsetWidth+gap;}"
                "var prev=wrap.querySelector('.v-carousel-prev');"
                "var next=wrap.querySelector('.v-carousel-next');"
                "if(prev)prev.addEventListener('click',function(){"
                "track.scrollBy({left:-step(),behavior:'smooth'});});"
                "if(next)next.addEventListener('click',function(){"
                "track.scrollBy({left:step(),behavior:'smooth'});});"
                "});"
                "})();</script>")
        else:
            websites_section = f"<h2>Your websites</h2>{biz_cards}"
        analyze_html = _analyze_box_html(prefill, show_prompt)
        # A customer with zero sites sees the analyze box first —
        # it is the way in. Everyone else reads websites first.
        if not snap["businesses"]:
            middle = f"{analyze_html}\n{websites_section}"
        else:
            middle = f"{websites_section}\n{analyze_html}"
        inner = f"""{banner}{_money_stats_html(account)}{_progress_grid_html(data)}
{decision_html}
{_agent_costs_card_html(account)}
{middle}
{_results_html(data)}
{_live_activity_feed_html(snap["events"])}
{footer}"""
        return _operator_page(
            "Overview", inner, account=account, current="live-ops",
            is_operator=is_op,
            extra_css=_PORTFOLIO_CSS + _REPORT_CSS)

    game = {
        "tick": 0,
        "experiment_001": None,
    }
    app.state.game = game

    def _agent_snapshot() -> list[dict]:
        # Derived from the audit log — the same source as the feed — not
        # from a second in-memory tally: the old game["agent_stats"]
        # only ever moved for discovery/research on a successful tick,
        # reset on every restart, and never saw the other five agents
        # or work done by other processes.
        orch = state["orchestrator"]
        aud: AuditLog = state["audit"]
        return agent_stats_from_audit(aud, orch.registry.agent_types())

    # -- professional Overview helpers --------------------------------------
    # The Overview is server-rendered through _operator_page (no
    # game.html, no JS poll). These helpers build its sections — and
    # the Agents page's moved sections — from real store data only.

    def _activity_events(account, limit: int = 50) -> list[dict]:
        """The activity feed, scoped to the viewer — the same events
        /api/snapshot returns, newest first. The operator (or no
        account, the tick internals) sees the audit log; a customer
        sees only their own jobs. The Overview's Live activity feed
        and the Agents page's "What just happened" both read this."""
        aud: AuditLog = state["audit"]
        if account is None or _saas_config.is_operator(account["email"]):
            return [{
                "ts": e.created_at.isoformat()
                if hasattr(e.created_at, "isoformat")
                else str(e.created_at),
                "actor": e.agent_type, "event": e.event,
                "business_id": e.business_id, "task_id": e.task_id,
                "result": e.result,
                "cost_usd": round(e.cost_usd, 4),
                "tokens_used": e.tokens_used,
            } for e in aud.latest(limit)]
        sites_by_id = {s["id"]: s
                       for s in _saas_store.list_sites(account["id"])}
        events = []
        for job in _saas_store.list_jobs(account["id"], limit=limit):
            site = sites_by_id.get(job["site_id"] or "")
            events.append({
                "ts": job["created_at"],
                "actor": "ventoryn",
                "event": "job_" + job["status"],
                "business_id": (site or {}).get("host", ""),
                "task_id": "",
                "result": {"job_type": job["job_type"],
                           "summary": job["result"] or ""},
                "cost_usd": 0.0,
                "tokens_used": 0,
            })
        return events

    def _fmt_event_time(ts) -> str:
        """An event timestamp as YYYY-MM-DD HH:MM (the feed is
        server-rendered, so the date matters — the old page only
        showed the clock). Falls back to the raw value, never
        crashes on a bad timestamp."""
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(str(ts))
            return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return str(ts or "")[:16]

    def _event_voice(e: dict) -> str:
        """One plain sentence for an activity event — the Agents
        page's "What just happened" feed. Professional wording:
        no crew / game voice. (Port of the old Live Ops eventVoice,
        minus the game framing.)"""
        r = e.get("result") or {}
        if isinstance(r, dict) and r.get("winner") is not None:
            tick = (f"T{r['tick']} — " if r.get("tick") is not None
                    else "")
            return (f"{tick}Analyst ranked the opportunities — "
                    f"'{r['winner']}' wins ({r.get('score', '—')})")
        if e.get("event") == "stop_loss_triggered":
            return "Stop-loss triggered — check Experiment 001."
        if e.get("event") == "tick_completed":
            who = ("Orchestrator" if e.get("actor") == "game"
                   else (e.get("actor") or "Someone"))
            return (who + " finished a tick" +
                    (" and launched a business." if e.get("business_id")
                     else "."))
        if isinstance(r, dict) and r.get("job_type"):
            done = e.get("event") == "job_done"
            return ((("" if done else "Failed: ") +
                     str(r["job_type"]).replace("_", " ") + " — " +
                     (r.get("summary") or e.get("event", ""))))
        return f"{e.get('actor') or 'system'} — {e.get('event') or ''}"

    def _event_result_text(e: dict) -> str:
        """The detail line for a Live activity feed row (port of the
        old eventResult): a winner summary, a plain string, or the
        result JSON truncated to 140 characters. Never a fake
        number."""
        r = e.get("result")
        if r is None:
            return ""
        if isinstance(r, str):
            return r
        if isinstance(r, dict):
            if r.get("winner") is not None:
                return (f"winner: '{r['winner']}' "
                        f"(score {r.get('score', '—')})")
            # Plain words, not raw JSON: {"tick": 4, "reports": 5}
            # reads "tick 4, reports 5"; {"passed": true} reads "passed".
            parts = []
            for k, v in r.items():
                label = str(k).replace("_", " ")
                if isinstance(v, bool):
                    parts.append(label if v else f"not {label}")
                elif isinstance(v, (int, float, str)):
                    parts.append(f"{label} {v}")
                elif isinstance(v, (list, tuple)):
                    parts.append(f"{label} ({len(v)})")
            s = ", ".join(parts)
            return s if len(s) <= 140 else s[:140] + "…"
        return str(r)

    def _live_activity_feed_html(events: list[dict],
                                 limit: int = 8) -> str:
        """The Live activity feed: one structured row per event —
        time, actor, event, business/task, cost + tokens, result.
        The Overview's own card (the feed lives here, not inside
        the "What the team is doing" panel — that panel moved to
        the Agents page). The latest 8 rows, with a link to the
        full feed on the Agents page."""
        rows = ""
        for e in events[:limit]:
            ids = " — ".join(x for x in (e.get("business_id"),
                                         e.get("task_id")) if x)
            cost = ""
            c = float(e.get("cost_usd") or 0)
            tk = int(e.get("tokens_used") or 0)
            if c > 0 or tk > 0:
                cost = f"${c:,.4f} — {tk:,} tokens"
            result = _event_result_text(e).replace(" · ", " — ")
            rows += (f"<div class='v-feed-row'>"
                     f"<span class='v-feed-time'>"
                     f"{esc(_fmt_event_time(e.get('ts', '')))}</span>"
                     f"<span class='v-feed-actor'>"
                     f"{esc(e.get('actor') or 'system')}</span>"
                     f"<span class='v-feed-event'>"
                     f"{esc(e.get('event') or '')}</span>"
                     + (f"<span class='v-feed-ids'>{esc(ids)}</span>"
                        if ids else "")
                     + (f"<span class='v-feed-cost'>{esc(cost)}</span>"
                        if cost else "")
                     + (f"<div class='v-feed-result'>{esc(result)}</div>"
                        if result else "")
                     + "</div>")
        if not rows:
            rows = ("<p class='muted'>Nothing yet — agent work will "
                    "show up here.</p>")
        return (f"<div class='card'><h2>Live activity</h2>{rows}"
                f"<p><a href='/agents'>View all on Agents</a></p></div>")

    def _team_doing_html(stats: list[dict], events: list[dict]) -> str:
        """What the team is doing (Agents page only): tasks & spend
        per agent (bars from the audit log) and activity over time
        (events per day, last 7 days, from the same persisted
        events — the old line chart was browser-memory poll
        history, which no longer exists, so the chart is rebuilt
        from what is actually stored)."""
        from datetime import datetime, timedelta, timezone

        rows = sorted(stats, key=lambda s: -int(s.get("tasks_completed", 0) or 0))
        max_tasks = max([int(s.get("tasks_completed", 0) or 0)
                         for s in rows] + [1])
        max_spend = max([float(s.get("cost_usd", 0) or 0)
                         for s in rows] + [0.0001])
        bars = ""
        for s in rows:
            tasks = int(s.get("tasks_completed", 0) or 0)
            spend = float(s.get("cost_usd", 0) or 0)
            tw = max(3 if tasks > 0 else 0, tasks / max_tasks * 100)
            sw = max(3 if spend > 0 else 0, spend / max_spend * 100)
            label = esc(str(s.get("type", "?")).replace("_", " ").title())
            bars += (
                f"<div class='v-team-bar'><span class='v-team-label'>"
                f"{label}</span>"
                f"<div class='v-team-track'><span class='v-team-fill' "
                f"style='width:{tw:.1f}%;background:var(--v-blue)'></span>"
                f"</div><span class='v-team-num'>{tasks} tasks</span></div>"
                f"<div class='v-team-bar'><span class='v-team-label'>"
                f"</span>"
                f"<div class='v-team-track'><span class='v-team-fill' "
                f"style='width:{sw:.1f}%;background:var(--v-violet-deep)'>"
                f"</span></div>"
                f"<span class='v-team-num'>${spend:,.4f}</span></div>")
        total_tasks = sum(int(s.get("tasks_completed", 0) or 0) for s in rows)
        total_spend = sum(float(s.get("cost_usd", 0) or 0) for s in rows)
        total_tokens = sum(int(s.get("tokens_used", 0) or 0) for s in rows)
        if total_tasks == 0 and total_spend == 0 and total_tokens == 0:
            bars += ("<p class='muted'>No work recorded yet — run a "
                     "tick and the bars will fill in.</p>")
        # Activity over time: events per day, last 7 days (UTC dates
        # — conversions and audit events are timestamped in UTC).
        today = datetime.now(timezone.utc).date()
        days = [today - timedelta(days=i) for i in range(6, -1, -1)]
        per_day = {d.isoformat(): 0 for d in days}
        for e in events:
            day = str(e.get("ts", ""))[:10]
            if day in per_day:
                per_day[day] += 1
        max_day = max(list(per_day.values()) + [1])
        cols = ""
        for d in days:
            n = per_day[d.isoformat()]
            h = max(2 if n > 0 else 0, n / max_day * 100)
            cols += (
                f"<div class='v-act-col'>"
                f"<span class='v-act-num'>{n}</span>"
                f"<span class='v-act-bar' style='height:{h:.0f}%'></span>"
                f"<span class='v-act-day'>{d.strftime('%b %d')}</span>"
                f"</div>")
        return (f"<div class='card'><h2>What the team is doing</h2>"
                f"{bars}"
                f"<h3>Activity — events per day, last 7 days</h3>"
                f"<div class='v-act'>{cols}</div>"
                f"<p class='muted'>{total_tasks} tasks, "
                f"{total_tokens:,} tokens, ${total_spend:,.4f} total "
                f"spend.</p></div>")

    def _analyze_box_html(prefill: str = "",
                          show_subscribe_prompt: bool = False) -> str:
        """The analyze-the-site box: URL + market country (+ optional
        language), POSTs to /api/analyze and polls for the result —
        the same async job as the landing page's free preview, in
        platform card styling. Nothing is published; the summary
        (scores and top findings) is shown here. The full written
        report comes only from the paid Analyze job on a website
        page (5 credits), never from this box. The element ids match
        the old Live Ops panel (analyze-panel, analyze-form,
        analyze-url, analyze-country, analyze-language,
        analyze-submit, analyze-status, analyze-result) so the box
        works the same everywhere it appears. `prefill` fills the URL
        from the landing funnel (?site_url=) so it is never retyped;
        when `show_subscribe_prompt` is true the box also offers the
        subscribe step for that URL."""
        prefill_attr = f" value='{esc(prefill)}'" if prefill else ""
        prompt = ""
        if show_subscribe_prompt and prefill:
            from urllib.parse import quote as _quote

            prompt = (
                f"<p><a href='/account/subscribe?site_url="
                f"{_quote(prefill, safe='')}'>Subscribe — $20/month "
                f"to add it</a></p>")
        return f"""<div class='card' id="analyze-panel">
<h2>Analyze a site</h2>
<p class='muted'>Paste a website URL and a market country — the same
analysis as the <code>analyze</code> command. Nothing is published;
the scores and top findings show here. The full written report comes
from the Analyze job on your website page (5 credits).</p>
<form id="analyze-form">
<div class='analyze-row'>
<label class='analyze-field analyze-url'>
<span class='analyze-label'>Website URL</span>
<input id="analyze-url" type='text' inputmode='url'
placeholder='yourwebsite.com' autocomplete='off' required{prefill_attr}>
</label>
<label class='analyze-field analyze-country'>
<span class='analyze-label'>Market country</span>
<select id="analyze-country" required>{country_options("ca")}</select>
</label>
<label class='analyze-field analyze-language'>
<span class='analyze-label'>Language (optional)</span>
<input id="analyze-language" type='text' placeholder='en'
maxlength='5' autocomplete='off'>
</label>
<button type='submit' class='btn btn-primary'
id="analyze-submit">Analyze</button>
</div>
</form>
{prompt}
<div id="analyze-status" class='analyze-status' aria-live='polite'></div>
<div id="analyze-result" hidden></div>
</div>
<script>
(function () {{
  var jobId = null;
  function $(id) {{ return document.getElementById(id); }}
  function esc(s) {{ return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }}
  function analyzeStatus(msg, isErr) {{
    var el = $("analyze-status");
    el.textContent = msg;
    el.classList.toggle("error", !!isErr);
  }}
  function renderAnalyzeResult(job) {{
    var box = $("analyze-result");
    var r = job.result || {{}};
    var html = "";
    if (r.fetchable === false) {{
      html += '<div class="analyze-scores">' +
        '<div class="analyze-score"><div class="label">Site score</div><div class="value na">Unavailable</div></div>' +
        '<div class="analyze-score"><div class="label">GEO score</div><div class="value na">Unavailable</div></div>' +
        "</div>" +
        "<p><strong>Could not fetch the site.</strong> " + esc(r.unavailable_reason || "Check the URL and try again.") + "</p>";
    }} else {{
      html += '<div class="analyze-scores">' +
        '<div class="analyze-score"><div class="label">Site score</div><div class="value">' + esc(r.site_score) + " / 100</div></div>" +
        '<div class="analyze-score"><div class="label">GEO score</div><div class="value">' + esc(r.geo_score) + " / 100</div></div>" +
        "</div>";
    }}
    var findings = r.findings || [];
    if (findings.length) {{
      html += '<h3>Top findings</h3><ul class="analyze-findings">';
      for (var i = 0; i < findings.length; i++) {{
        var f = findings[i];
        html += "<li><span class=\\"sev\\">[" + esc(f.severity) + " — " + esc(f.area) + "]</span> " +
          esc(f.issue) + " — Fix: " + esc(f.fix) + "</li>";
      }}
      html += "</ul>";
    }} else if (r.fetchable !== false) {{
      html += "<p>No findings — the site is in good shape for this market.</p>";
    }}
    box.innerHTML = html;
    box.hidden = false;
  }}
  async function pollAnalyzeJob() {{
    if (!jobId) return;
    try {{
      var res = await fetch("/api/analyze/" + encodeURIComponent(jobId));
      var job = await res.json();
      if (job.status === "done") {{
        jobId = null;
        analyzeStatus("Done — " + job.url);
        renderAnalyzeResult(job);
        $("analyze-submit").disabled = false;
      }} else if (job.status === "failed") {{
        jobId = null;
        analyzeStatus("Could not analyze: " + (job.error || "the analysis failed."), true);
        $("analyze-submit").disabled = false;
      }} else {{
        analyzeStatus("Analyzing " + job.url + "… (this takes a few seconds)");
        setTimeout(pollAnalyzeJob, 3000);
      }}
    }} catch (err) {{
      if (String(err.message || "").indexOf("404") !== -1) {{
        jobId = null;
        analyzeStatus("The dashboard restarted while the analysis was running — please run it again.", true);
        $("analyze-submit").disabled = false;
      }} else {{
        setTimeout(pollAnalyzeJob, 3000);
      }}
    }}
  }}
  $("analyze-form").addEventListener("submit", async function (ev) {{
    ev.preventDefault();
    var url = $("analyze-url").value.trim();
    if (url && url.indexOf("://") === -1) url = "https://" + url;
    var country = $("analyze-country").value.trim().toLowerCase();
    var language = $("analyze-language").value.trim().toLowerCase();
    $("analyze-result").hidden = true;
    $("analyze-result").innerHTML = "";
    analyzeStatus("Starting analysis…");
    $("analyze-submit").disabled = true;
    try {{
      var res = await fetch("/api/analyze", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ url: url, country: country, language: language }}),
      }});
      var data = await res.json();
      if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
      jobId = data.job_id;
      analyzeStatus("Analyzing " + data.url + "… (this takes a few seconds)");
      setTimeout(pollAnalyzeJob, 3000);
    }} catch (err) {{
      analyzeStatus("Could not start the analysis: " + err.message, true);
      $("analyze-submit").disabled = false;
    }}
  }});
}})();
</script>"""

    # Inline SVG for a flow step with no evidence yet (a grey dot,
    # matching the green check from ventoryn.icons).
    _FLOW_DOT_SVG = (
        "<svg width='16' height='16' viewBox='0 0 24 24' fill='none' "
        "aria-hidden='true'><circle cx='12' cy='12' r='5' "
        "fill='currentColor' stroke='none'/></svg>")

    def _attribution_data(account, *, check_visitors: bool,
                          return_to: str = "/report") -> dict:
        """The Acquisition & Attribution computation, in one place:
        the /report customer branch, the new /campaigns page, the
        /analytics page and the /api/snapshot payload all read it,
        so the figures can never disagree.

        ``check_visitors`` is True only for the /report page: the
        "Website Visit" step then does a LIVE Plausible fetch and
        is checked only where visitors were actually recorded. The
        snapshot path (polled every 5s) and the other pages pass
        False — there the step's evidence is the connection status
        only ("Plausible is connected for this site"), and no
        network fetch happens. Honest either way; nothing is
        estimated.

        ``return_to`` is where the privacy forms post back to."""
        from ventoryn.saas import plausible_visitors

        sites = _saas_store.list_sites(account["id"])
        site_ids = [s["id"] for s in sites]
        counted_ids = [s["id"] for s in sites
                       if s.get("count_show", 1)]
        counts = _saas_store.conversion_status_counts(counted_ids)
        verified = counts["verified"]
        total_conv = counts["total"]
        last7 = _saas_store.verified_in_last_days(counted_ids, days=7)
        trend = _saas_store.verified_trend(counted_ids, days=30)
        any_count_hidden = len(counted_ids) != len(site_ids)

        # Progress ring (SVG): verified-customer count.
        pct = min(verified, 100) / 100
        circ = 326.7
        ring = (
            f"<svg width='140' height='140' viewBox='0 0 140 140' "
            f"role='img' aria-label='Verified customers: {verified}'>"
            f"<circle cx='70' cy='70' r='52' "
            f"fill='none' stroke='#e9e4fb' stroke-width='14'/>"
            f"<circle cx='70' cy='70' r='52' fill='none' "
            f"stroke='#8b5cf6' stroke-width='14' stroke-linecap='round' "
            f"stroke-dasharray='{circ}' "
            f"stroke-dashoffset='{circ * (1 - pct):.1f}' "
            f"transform='rotate(-90 70 70)'/>"
            f"<text x='70' y='66' text-anchor='middle' font-size='28' "
            f"font-weight='800' fill='#6d28d9'>{verified}</text>"
            f"<text x='70' y='86' text-anchor='middle' font-size='13' "
            f"fill='#666'>verified</text></svg>")

        # Attribution flow: a step shows a check only where this
        # account has real evidence for it.
        jobs = _saas_store.list_jobs(account["id"])
        gate = state["approvals"]
        site_biz_ids = {f"site:{s['id']}" for s in sites}
        decided_here = [
            a for a in gate.decided(limit=100)
            if getattr(a, "business_id", None) in site_biz_ids]
        any_sig = any(
            _saas_store.list_signatures(s["id"]) for s in sites)
        # Strongest evidence first: Ventoryn Analytics saw a visit that
        # arrived through a signed Ventoryn link on one of these sites.
        from ventoryn.analytics import analytics_for
        _va = analytics_for(_saas_store)
        signed_visit = any(_va.summary(s["id"])["signed_visits"]
                           for s in sites)
        if signed_visit:
            visit_ok = True
            visit_note = ("Ventoryn Analytics recorded a visit through a "
                          "signed Ventoryn link.")
        elif check_visitors:
            has_visitors = False
            for s in sites:
                conn = _saas_store.get_connection(s["id"], "plausible") or {}
                if conn.get("status") == "connected":
                    raw = conn.get("_detail_raw") or {}
                    key = raw.get("api_key")
                    if key:
                        v = plausible_visitors(
                            key, raw.get("site_domain") or s["host"])
                        if v:
                            has_visitors = True
                            break
            visit_ok = has_visitors
            visit_note = "Plausible recorded visitors on a connected site."
        else:
            # No live fetch: the connection status is the evidence.
            visit_ok = any(
                (_saas_store.get_connection(s["id"], "plausible") or {}
                 ).get("status") == "connected" for s in sites)
            visit_note = "Plausible is connected for this site."
        any_stripe = any(
            c.get("stripe_event_id") or c.get("source") != "manual"
            for s in sites
            for c in _saas_store.list_conversions(s["id"], limit=1000))
        _flow = [
            ("AI Recommendation", bool(jobs),
             "A job ran for this account."),
            ("Customer Approval", bool(decided_here),
             "An approval was decided by this account."),
            ("Signed Campaign Link", any_sig,
             "A signed Ventoryn link was created."),
            ("Website Visit", visit_ok, visit_note),
            ("Stripe Payment", any_stripe,
             "A Stripe payment was recorded."),
            ("Verified Conversion", verified > 0,
             "A signed sale was verified."),
        ]
        flow_steps = [
            {"label": label, "ok": ok,
             "note": note if ok else "No evidence yet"}
            for label, ok, note in _flow]

        # Conversion status donut (SVG, three arcs + total).
        if total_conv:
            import math
            dc = 2 * math.pi * 54
            segs = [("Verified", counts["verified"], "#16a34a"),
                    ("Pending", counts["pending"], "#d97706"),
                    ("Unattributed", counts["unattributed"], "#9ca3af")]
            arcs = ""
            offset = 0.0
            for _label, n, color in segs:
                if n == 0:
                    continue
                length = dc * n / total_conv
                arcs += (
                    f"<circle cx='70' cy='70' r='54' fill='none' "
                    f"stroke='{color}' stroke-width='22' "
                    f"stroke-dasharray='{length:.1f} {dc - length:.1f}' "
                    f"stroke-dashoffset='{-offset:.1f}' "
                    f"transform='rotate(-90 70 70)'/>")
                offset += length
            donut = (
                f"<svg width='140' height='140' viewBox='0 0 140 140' "
                f"role='img' aria-label='Conversion status: "
                f"{counts['verified']} verified, {counts['pending']} "
                f"pending, {counts['unattributed']} unattributed'>"
                f"<circle cx='70' cy='70' r='54' fill='none' "
                f"stroke='#f3f4f6' stroke-width='22'/>{arcs}"
                f"<text x='70' y='66' text-anchor='middle' "
                f"font-size='26' font-weight='800' fill='#1a1a2e'>"
                f"{total_conv}</text>"
                f"<text x='70' y='84' text-anchor='middle' "
                f"font-size='12' fill='#666'>total</text></svg>")
            legend = "".join(
                f"<li><span class='v-dot' style='background:{color}'>"
                f"</span>{label}: <strong>{n}</strong> "
                f"({(100 * n / total_conv):.1f}%)</li>"
                for label, n, color in segs)
        else:
            donut = (
                "<svg width='140' height='140' viewBox='0 0 140 140' "
                "role='img' aria-label='Conversion status: no "
                "conversions yet'><circle cx='70' cy='70' r='54' "
                "fill='none' stroke='#f3f4f6' stroke-width='22'/>"
                "<text x='70' y='75' text-anchor='middle' font-size='14' "
                "fill='#666'>No data</text></svg>")
            legend = "<li>No conversions recorded yet.</li>"

        # Campaign performance: one row per signed action.
        campaigns = {}
        for s in sites:
            for sig in _saas_store.list_signatures(s["id"], limit=1000):
                key = sig["action_id"] or sig["tag"]
                if key not in campaigns:
                    campaigns[key] = {
                        "url": sig["target_url"], "agent": sig["agent"],
                        "verified": 0, "action_id": key}
                campaigns[key]["verified"] += sum(
                    1 for c in _saas_store.list_conversions(
                        s["id"], limit=1000)
                    if c.get("action_id") == sig["action_id"]
                    and c.get("attribution") == "ventoryn"
                    and not c.get("refunded_at") and not c.get("disputed_at"))
        campaign_list = list(campaigns.values())

        # Acquisition trend: cumulative verified by day.
        # Real data only — no estimated/projection line is drawn.
        if trend:
            vmax = max(100, max(p["verified"] for p in trend))
            tw, th = 600, 160
            npts = len(trend)
            pts = " ".join(
                f"{(i * tw / max(npts - 1, 1)):.1f},"
                f"{(th - (p['verified'] / vmax) * th):.1f}"
                for i, p in enumerate(trend))
            trend_svg = (
                f"<svg viewBox='0 0 {tw} {th}' class='v-trend' "
                f"role='img' aria-label='Acquisition trend: verified "
                f"customers over the last 30 days'>"
                f"<polyline points='{pts}' fill='none' "
                f"stroke='#8b5cf6' stroke-width='2.5'/></svg>"
                f"<p class='muted'>Verified customers, cumulative, "
                f"last 30 days. "
                f"No projection is drawn, only real verified sales.</p>")
        else:
            trend_svg = ("<p class='muted'>No verified customers yet — "
                         "the trend appears once the first signed sale "
                         "lands.</p>")

        # Conversion breakdown bars.
        maxb = max(counts["verified"], counts["pending"],
                   counts["unattributed"], 1)
        breakdown_html = "".join(
            f"<div class='v-bbar'><span class='v-bbar-label'>{label}"
            f"</span><span class='v-bbar-track'><span "
            f"class='v-bbar-fill' style='width:{(100 * n / maxb):.0f}%;"
            f"background:{color}'></span></span>"
            f"<span class='v-bbar-num'>{n}</span></div>"
            for label, n, color in [
                ("Verified", counts["verified"], "#16a34a"),
                ("Pending", counts["pending"], "#d97706"),
                ("Unattributed", counts["unattributed"], "#9ca3af")])

        # Data & privacy controls: one form per site, posting to
        # the same privacy endpoint as the site page (fresh 2FA
        # code required there when 2FA is enabled).
        def _chk(on):
            return "checked" if on else ""
        totp_field = ""
        if account.get("totp_enabled"):
            totp_field = (
                "<label>Two-factor code (fresh — required to confirm "
                "this change)</label><input type='text' "
                "name='totp_code' inputmode='numeric' "
                "autocomplete='one-time-code' maxlength='8'>")
        privacy_forms = ""
        for s in sites:
            privacy_forms += (
                f"<div class='card'><h3>{esc(s['url'])}</h3>"
                f"<form method='post' action='/account/sites/{esc(s['id'])}/privacy'>"
                f"<input type='hidden' name='return_to' value='{esc(return_to)}'>"
                f"<label><input type='checkbox' name='revenue_show' "
                f"{_chk(s.get('revenue_show'))}> Show my revenue on "
                f"Ventoryn</label>"
                f"<label><input type='checkbox' name='count_show' "
                f"{_chk(s.get('count_show', 1))}> Show my customer "
                f"count</label>"
                f"<label><input type='checkbox' name='details_show' "
                f"{_chk(s.get('details_show', 1))}> Show conversion "
                f"details</label>"
                f"{totp_field}"
                "<p><button class='btn' type='submit'>Save privacy "
                "settings</button></p></form></div>")
        if not privacy_forms:
            privacy_forms = ("<p class='muted'>No websites yet — add "
                             "one to set its privacy controls.</p>")

        # Your websites (revenue/count hidden per the toggles, via
        # revenue_for_site) — the display-ready HTML per site.
        site_list = []
        for s in sites:
            rev = _saas_store.revenue_for_site(s["id"])
            conn = _saas_store.get_connection(s["id"], "stripe_sales") or {}
            stripe_connected = conn.get("status") == "connected"
            if rev is None:
                count_html = ("<span class='unavailable'>"
                              "unavailable</span>")
                revenue_html = ("<span class='unavailable'>"
                                "unavailable</span>")
            else:
                count_html = (
                    f"{rev['paying_customers']:,}"
                    if rev["paying_customers"] is not None else
                    "<span class='unavailable'>hidden</span>")
                if not stripe_connected:
                    revenue_html = ("<span class='unavailable'>"
                                    "unavailable — connect Stripe "
                                    "sales</span>")
                elif rev["revenue"] is None:
                    revenue_html = ("<span class='unavailable'>"
                                    "hidden</span>")
                else:
                    revenue_html = (
                        f"${rev['revenue']['total_cents'] / 100:,.2f} "
                        f"{rev['revenue']['currency'].upper()}")
            site_list.append({
                "id": s["id"], "url": s["url"], "host": s["host"],
                "count_show": bool(s.get("count_show", 1)),
                "revenue_show": bool(s.get("revenue_show")),
                "details_show": bool(s.get("details_show", 1)),
                "stripe_connected": stripe_connected,
                "count_html": count_html,
                "revenue_html": revenue_html,
            })
        return {
            "counts": counts, "verified": verified,
            "total": total_conv, "last7": last7, "trend": trend,
            "any_count_hidden": any_count_hidden,
            "ring_svg": ring, "donut_svg": donut,
            "legend_html": legend, "flow_steps": flow_steps,
            "campaigns": campaign_list, "trend_svg": trend_svg,
            "breakdown_html": breakdown_html,
            "sites": site_list, "privacy_forms": privacy_forms,
        }

    def _snapshot(account=None) -> dict:
        """The Live Ops snapshot, scoped to the viewer.

        account=None (the tick/reset internals) or the operator
        account -> the full runtime: every registry business, the
        audit feed, the agent bars, Experiment 001. A regular
        customer -> ONLY their own sites (as business rows), their
        own jobs as the activity feed, zeroed agent bars (no agent
        run is recorded against their account yet), and no
        Experiment 001. Never another account's sites, never global
        revenue."""
        reg: BusinessRegistry = state["registry"]
        led: Ledger = state["ledger"]
        operator = account is None or _saas_config.is_operator(
            account["email"])
        if operator:
            # Tick/reset internals (account None) see the whole
            # runtime; a signed-in operator sees the businesses their
            # account owns plus unclaimed ones — never another
            # account's.
            businesses = (reg.list() if account is None
                          else _visible_businesses(account))
            biz_rows = [_biz_row(b) for b in businesses]
            if account is not None:
                # The operator's own SaaS sites ride along with his
                # businesses — his account sees everything of his.
                biz_rows += _site_business_rows(account)
            events = _activity_events(account, limit=50)
            agents = _agent_snapshot()
            exp = game["experiment_001"]
        else:
            biz_rows = _site_business_rows(account)
            events = _activity_events(account, limit=50)
            agents = [{
                "type": t, "tasks_completed": 0, "tokens_used": 0,
                "cost_usd": 0.0, "last_task": "",
            } for t in state["orchestrator"].registry.agent_types()]
            exp = None
        # A hidden revenue is not $0 and not a number at all: if
        # any business's revenue is hidden, the totals are hidden
        # too (the true total includes amounts the customer chose not
        # to show). Callers render None as "hidden".
        any_hidden = any(r["revenue"] is None for r in biz_rows)
        totals = {
            "revenue": (None if any_hidden else
                        round(sum(r["revenue"] for r in biz_rows), 2)),
            "total_costs": round(sum(r["total_costs"] for r in biz_rows), 2),
            "net_profit": (None if any_hidden else
                           round(sum(r["net_profit"] for r in biz_rows), 2)),
            "businesses": len(biz_rows),
        }
        # Attribution (DJINN reads /api/snapshot too): the JSON-safe
        # subset of _attribution_data — counts, verified, last 7
        # days, trend points, campaigns, flow steps. check_visitors
        # is False here: the page polls this endpoint every 5s, so
        # it must NEVER do a live Plausible fetch; the "Website
        # Visit" step's evidence is the connection status only.
        # Additive only — no existing key changes. With no account
        # (tick/reset internals) there is no account to scope it
        # to, so the key reads null.
        attribution = None
        if account is not None:
            _att = _attribution_data(account, check_visitors=False)
            attribution = {
                "counts": _att["counts"],
                "verified": _att["verified"],
                "last7": _att["last7"],
                "trend": _att["trend"],
                "campaigns": [
                    {"url": c["url"], "agent": c["agent"],
                     "verified": c["verified"]}
                    for c in _att["campaigns"]],
                "flow_steps": _att["flow_steps"],
            }
        return {
            "tick": game["tick"],
            "businesses": biz_rows,
            "agents": agents,
            "totals": totals,
            "events": events,
            "experiment_001": exp,
            "account": _account_json(account) if account else None,
            "attribution": attribution,
        }

    @app.get("/api/snapshot")
    def api_snapshot(request: Request):
        """Live Ops data, scoped to the signed-in account. Sign-in
        required: an unsigned caller gets 401, not the global
        portfolio — the snapshot is how a customer would otherwise
        read revenue that is not theirs. (DJINN's read-only
        integration calls this too; it needs an operator session.)"""
        account, redirect = _need_account(request)
        if redirect is not None:
            return JSONResponse({"detail": "sign-in required"},
                                status_code=401)
        return JSONResponse(_snapshot(account))

    @app.get("/api/report")
    def api_report(request: Request):
        """Agent + financial report as JSON. Composed from _snapshot() so the
        composition logic lives in one place; blended margin comes from
        core.reporting.FinancialReporting. Scoped like the snapshot:
        a customer gets only their own sites' numbers."""
        from core.reporting import FinancialReporting
        from orchestrator.models import utcnow

        account, redirect = _need_account(request)
        if redirect is not None:
            return JSONResponse({"detail": "sign-in required"},
                                status_code=401)
        snap = _snapshot(account)
        exp = snap["experiment_001"]
        if _saas_config.is_operator(account["email"]):
            summary = FinancialReporting(state["ledger"]).portfolio_summary(
                [b["id"] for b in snap["businesses"]
                 if not str(b["id"]).startswith("site:")])
            margin = round(summary["blended_margin"], 4)
        else:
            # No ledger rows for a customer's sites — margin is
            # undefined, not zero, so it reads as null.
            margin = None
        return JSONResponse({
            "generated_at": utcnow().isoformat(),
            "tick": snap["tick"],
            "agents": snap["agents"],
            "financials": {
                "revenue": snap["totals"]["revenue"],
                "total_costs": snap["totals"]["total_costs"],
                "net_profit": snap["totals"]["net_profit"],
                "blended_margin": margin,
            },
            "businesses": snap["businesses"],
            "experiment_001": {
                "verdict": exp["verdict"],
                "days_elapsed": exp["days_elapsed"],
                "days_remaining": exp["days_remaining"],
                "orders": exp["orders"],
                "revenue_usd": exp["revenue_usd"],
                "spend_usd": exp["spend_usd"],
                "pace_pct": exp["pace_pct"],
            } if exp else None,
        })

    def _growth_api_gate(request: Request):
        """The Road to CISSP growth APIs expose that business's data:
        only the account that manages it may read them. Returns a
        JSONResponse to send back, or None when allowed."""
        from businesses.road2cissp.growth import BUSINESS_NAME

        account = _current_account(request)
        if account is None:
            return JSONResponse({"detail": "sign-in required"},
                                status_code=401)
        if not any(b.name == BUSINESS_NAME
                   for b in _visible_businesses(account)):
            return JSONResponse({"detail": "not found"}, status_code=404)
        return None

    @app.get("/api/daily-plan")
    def api_daily_plan(request: Request):
        """Today's ordered marketing moves for Road to CISSP, as JSON.

        Pure read + build from the growth state file — no agent is
        dispatched, no money is spent, nothing is published. The state
        file is the default growth state unless DAILY_PLAN_STATE_PATH
        points elsewhere (tests use that to point at a tmp file).
        Building the plan here does not persist it; the CLI
        (``daily-plan``) records it so tomorrow can carry items over.
        """
        denied = _growth_api_gate(request)
        if denied is not None:
            return denied
        from ecosystem import daily_plan
        from businesses.road2cissp.growth import (
            CADENCE, RETRY_AFTER, default_state_path, load_state,
        )

        state_path = os.environ.get("DAILY_PLAN_STATE_PATH")
        state = load_state(Path(state_path) if state_path
                           else default_state_path())
        from datetime import datetime, timezone

        plan = daily_plan.build_daily_plan(
            business_name="Road to CISSP", site_url="https://road2cissp.com",
            state=state, now=datetime.now(timezone.utc),
            cadence=CADENCE, retry_after=RETRY_AFTER)
        return JSONResponse(plan)

    @app.get("/api/attribution")
    def api_attribution(request: Request):
        """Per-channel funnel attribution for Road to CISSP, as JSON.

        Pure read from the growth state file (same file the daily
        plan reads, ``DAILY_PLAN_STATE_PATH`` when set): recorded
        conversions attributed per channel, sessions from the latest
        recorded Plausible snapshot. No Plausible fetch here, no agent
        dispatched, no money spent. The CLI (``attribution``) is the
        one that fetches fresh Plausible data and writes the report.
        """
        denied = _growth_api_gate(request)
        if denied is not None:
            return denied
        from core.attribution import build_attribution
        from businesses.road2cissp.growth import (
            default_state_path, load_state,
        )

        state_path = os.environ.get("DAILY_PLAN_STATE_PATH")
        state = load_state(Path(state_path) if state_path
                           else default_state_path())
        return JSONResponse(build_attribution(state))

    # -- Site analysis jobs (powers the /ops "Analyze a site" panel) ---
    # The same pipeline as the `analyze` CLI, run in a background
    # thread: POST starts the job, the page polls GET for the result.
    # Nothing is published or posted — the only output is the on-page
    # summary (site score, GEO score, top findings). This endpoint
    # NEVER writes or serves the full report file: that comes only
    # from the paid Analyze job on a website page (5 credits, POST
    # /account/sites/{site_id}/jobs). Anonymous previews are
    # rate-limited per client IP (see
    # ventoryn.analyze_jobs.PREVIEW_PER_IP_PER_HOUR); signed-in
    # users are not limited here — their usage is attributed to an
    # account. `app.state.analyze_runner` is None in production;
    # tests inject a fast fake runner.
    from ventoryn.analyze_jobs import get_job as _analyze_get_job
    from ventoryn.analyze_jobs import new_job_store as _analyze_new_store
    from ventoryn.analyze_jobs import preview_allowed as _preview_allowed
    from ventoryn.analyze_jobs import preview_client_ip as _preview_ip
    from ventoryn.analyze_jobs import record_preview_hit as _preview_hit
    from ventoryn.analyze_jobs import start_job as _analyze_start_job
    from ventoryn.analyze_jobs import validate_country as _validate_country
    from ventoryn.analyze_jobs import validate_language as _validate_language
    from ventoryn.analyze_jobs import validate_url as _validate_analyze_url

    app.state.analyze_jobs = _analyze_new_store()
    # The free analysis is public and makes outbound fetches: cap it
    # per client, and cap how many run at once across everyone.
    # (Anonymous previews: 5 per client IP per hour, in analyze_jobs.)
    from ventoryn.security import RateLimiter
    app.state.analyze_limit_account = RateLimiter(limit=30, window=600)
    app.state.analyze_max_running = 4
    app.state.analyze_runner = None

    @app.post("/api/analyze")
    def api_analyze_start(request: Request, payload: dict):
        # The preview endpoint: summary only, for everyone. The full
        # report comes only from the paid Analyze job on a website
        # page — this endpoint never writes or serves one (enforced
        # in ventoryn.analyze_jobs: preview jobs carry no report).
        # Anonymous previews are rate-limited per client IP so the
        # free endpoint cannot be hammered; signed-in usage is
        # attributed to an account and gets a generous per-account
        # limit; and at most a few analyses run at once overall.
        payload = payload or {}
        try:
            url = _validate_analyze_url(payload.get("url", ""))
            country = _validate_country(payload.get("country", ""))
            language = _validate_language(payload.get("language", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        store = app.state.analyze_jobs
        with store["lock"]:
            running = sum(1 for j in store["jobs"].values()
                          if j["status"] in ("queued", "running"))
        if running >= app.state.analyze_max_running:
            return JSONResponse(
                {"detail": "Ventoryn is busy analyzing other sites — "
                           "try again in a minute."},
                status_code=429, headers={"Retry-After": "60"})
        account = _current_account(request)
        if account is not None:
            key = f"acct:{account['id']}"
            if not app.state.analyze_limit_account.allow(key):
                wait = app.state.analyze_limit_account.retry_after(key)
                return JSONResponse(
                    {"detail": "Too many analyses from this account — try "
                               f"again in {max(1, wait // 60)} minute(s)."},
                    status_code=429, headers={"Retry-After": str(wait)})
        anonymous = _current_account(request) is None
        ip = None
        if anonymous:
            ip = _preview_ip(
                request.headers,
                request.client.host if request.client else "unknown")
            allowed, retry_after = _preview_allowed(
                app.state.analyze_jobs, ip)
            if not allowed:
                return JSONResponse(
                    {"detail": "Free preview limit reached — 5 previews "
                     "per hour from this address. Sign in to run the "
                     "full analysis, or try again later."},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)})
        job = _analyze_start_job(
            app.state.analyze_jobs, url, country, language,
            runner=app.state.analyze_runner, preview=True)
        if anonymous:
            _preview_hit(app.state.analyze_jobs, ip)
        return JSONResponse(
            {"job_id": job["job_id"], "status": job["status"],
             "url": job["url"], "country": job["country"],
             "language": job["language"]}, status_code=202)

    @app.get("/api/analyze/{job_id}")
    def api_analyze_status(job_id: str):
        job = _analyze_get_job(app.state.analyze_jobs, job_id)
        if job is None:
            raise HTTPException(404, "analysis job not found")
        out = dict(job)
        if out.get("report_name"):
            out["report_url"] = f"/api/analyze/{job_id}/report"
        return JSONResponse(out)

    @app.get("/api/analyze/{job_id}/report")
    def api_analyze_report(job_id: str):
        from ventoryn.reports import reports_dir

        job = _analyze_get_job(app.state.analyze_jobs, job_id)
        if job is None or not job.get("report_name"):
            raise HTTPException(404, "report not ready")
        base = reports_dir()
        path = (base / job["report_name"]).resolve()
        if path.parent != base.resolve() or not path.is_file():
            raise HTTPException(404, "report not ready")
        return FileResponse(str(path), media_type="text/markdown",
                            filename=job["report_name"])

    def _progress_grid_html(data: dict) -> str:
        """The top summary: progress ring (verified-customer count) + the
        Verified / Pending / Unattributed stat cards. Shared by the
        customer /report and the professional Overview, so the two
        can never disagree."""
        counts = data["counts"]
        verified = data["verified"]
        last7 = data["last7"]
        ring = data["ring_svg"]
        count_note = (
            "<p class='muted'>The customer count is hidden on one or "
            "more of your sites — change it in Data &amp; privacy "
            "controls below.</p>" if data["any_count_hidden"] else "")
        return f"""<div class='v-report-grid'>
<div class='card v-progress'><h2>Verified customers</h2>{ring}
<div class='v-bar-track'><div class='v-bar-fill' style='width:{min(verified, 100)}%'></div></div>
<p><span class='pill green'>+{last7} verified customers (7 days)</span></p>
<p class='muted'>Verified paying customers, counted from signed sales.</p>{count_note}</div>
<div class='card v-kpi-stack'><h2>Customers</h2>
<div class='v-kpi-row'><div><strong>Verified</strong><p class='muted'>The Ventoryn signature reached the payment.</p></div><span class='big'>{verified}</span></div>
<div class='v-kpi-row'><div><strong>Pending</strong><p class='muted'>Owner-recorded, not yet verified by Stripe.</p></div><span class='big'>{counts['pending']}</span></div>
<div class='v-kpi-row'><div><strong>Unattributed</strong><p class='muted'>Real Stripe payment, no signature.</p></div><span class='big'>{counts['unattributed']}</span></div>
</div>
</div>"""

    def _flow_html(data: dict) -> str:
        """The attribution flow as a horizontal stepper: one card
        per step, a green check where this account has real evidence,
        a grey dot where it does not. Shared by /report and the
        Overview Results section."""
        return "".join(
            f"<div class='v-flow-step{' done' if step['ok'] else ''}'>"
            f"<span class='v-flow-check'>"
            f"{icon('check', 16) if step['ok'] else _FLOW_DOT_SVG}"
            f"</span> <strong>{esc(step['label'])}</strong>"
            f"<span>{esc(step['note'])}</span></div>"
            + (f"<span class='v-flow-arrow'>{icon('arrow_right', 18)}"
               f"</span>" if i < 5 else "")
            for i, step in enumerate(data["flow_steps"]))

    def _campaign_rows_html(data: dict) -> str:
        """The campaign table rows: one row per signed campaign
        (url, agent, verified conversions), or the honest empty row.
        Shared by /report and the Overview Results section."""
        return "".join(
            f"<tr><td>{esc(c['url'])}</td><td>{esc(c['agent'])}</td>"
            f"<td>{c['verified']}</td><td>Active</td></tr>"
            for c in data["campaigns"]) or (
            "<tr><td colspan='4'>No signed campaigns yet — run agent "
            "work from a site page.</td></tr>")

    def _results_html(data: dict) -> str:
        """The merged Results section on Overview: the attribution
        flow as a horizontal stepper, the conversion donut + campaign
        table side by side in one card, then the trend chart, then
        one muted data-source line. The privacy card and the
        duplicate "Conversion breakdown" card do NOT live here — the
        donut already is the breakdown, and privacy is a quiet
        footer link on Overview. /report keeps
        _attribution_sections_html unchanged."""
        return f"""<div class='card'><h2>Results</h2>
<h3>Attribution flow</h3><div class='v-flow'>{_flow_html(data)}</div>
<p class='muted'>A step shows a check only where this account has real
evidence for it. Steps without evidence read "No evidence yet" — nothing
is assumed.</p>
<div class='v-results-pair'>
<div><h3>Conversion status</h3>
<div class='v-donut-wrap'>{data['donut_svg']}<ul class='v-legend'>{data['legend_html']}</ul></div></div>
<div><h3>Campaign performance</h3>
<div class='table-wrap'><table>
<tr><th>Campaign</th><th>Source</th><th>Verified conversions</th><th>Status</th></tr>
{_campaign_rows_html(data)}</table></div>
<p class='muted'>Clicks per campaign are not tracked yet — the table shows
verified conversions only.</p></div>
</div>
<h3>Acquisition trend</h3>{data['trend_svg']}
<p class='muted'>All figures are real or marked unavailable — nothing is
estimated.</p></div>"""

    def _attribution_sections_html(data: dict) -> str:
        """The attribution sections shared by the customer /report
        and the professional Overview: the flow, the conversion
        donut + campaign table, the privacy controls, the trend +
        breakdown + data-source legend. Every figure comes from
        _attribution_data — this only renders it."""
        flow_html = _flow_html(data)
        camp_rows = _campaign_rows_html(data)
        return f"""<div class='card'><h2>Attribution flow</h2><div class='v-flow'>{flow_html}</div>
<p class='muted'>A step shows a check only where this account has real
evidence for it. Steps without evidence read "No evidence yet" — nothing
is assumed.</p></div>
<div class='v-report-grid'>
<div class='card'><h2>Conversion status</h2>
<div class='v-donut-wrap'>{data['donut_svg']}<ul class='v-legend'>{data['legend_html']}</ul></div></div>
<div class='card'><h2>Campaign performance</h2>
<div class='table-wrap'><table>
<tr><th>Campaign</th><th>Source</th><th>Verified conversions</th><th>Status</th></tr>
{camp_rows}</table></div>
<p class='muted'>Clicks per campaign are not tracked yet — the table shows
verified conversions only.</p></div>
</div>
<div class='card'><h2>Data &amp; privacy controls</h2>
<p class='muted'>Revenue amounts are private by default. The count and the
per-sale details are separate settings.</p>{data['privacy_forms']}</div>
<div class='v-report-grid'>
<div class='card'><h2>Acquisition trend</h2>{data['trend_svg']}</div>
<div class='card'><h2>Conversion breakdown</h2>{data['breakdown_html']}
<div class='card'><h2>Data source &amp; accuracy</h2><ul>
<li><strong>Real data</strong> — live, verified events (e.g. Stripe, analytics).</li>
<li><strong>Estimated data</strong> — Ventoryn does not show estimated data on this report — figures are real or unavailable.</li>
<li><strong>Demonstration data</strong> — sample data shown for illustration only — used only on the public landing preview.</li>
</ul></div></div>
</div>"""

    @app.get("/report", response_class=HTMLResponse)
    def report_page(request: Request):
        """Server-rendered report (mobile-friendly). The operator gets
        the agent + financial report; a customer gets their own
        growth report — their sites, funnel, jobs, credits — never
        another account's numbers."""
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        from orchestrator.models import utcnow

        snap = _snapshot(account)
        exp = snap["experiment_001"]
        fin = {
            "revenue": snap["totals"]["revenue"],
            "total_costs": snap["totals"]["total_costs"],
            "net_profit": snap["totals"]["net_profit"],
        }
        def _money(value):
            return "hidden" if value is None else money(value)

        if not _saas_config.is_operator(account["email"]):
            # Acquisition & Attribution (reference image 3): the
            # product's attribution-first view. Every figure is real
            # and tagged with its source; the count toggle is
            # respected per site — a site with the count hidden
            # contributes nothing to the displayed counts. The
            # computation lives in _attribution_data (shared with
            # /campaigns, /analytics and the snapshot); this branch
            # only renders it. check_visitors=True here: the strict
            # read — visitors actually recorded.
            data = _attribution_data(account, check_visitors=True)
            # Your websites + recent jobs (revenue/count hidden per
            # the toggles — the display HTML comes from the helper).
            site_rows = "".join(
                f"<tr><td>{esc(s['url'])}</td><td>{s['count_html']}</td>"
                f"<td>{s['revenue_html']}</td></tr>"
                for s in data["sites"]) or (
                "<tr><td colspan='3'>No websites yet.</td></tr>")
            job_rows = "".join(
                f"<tr><td>{esc(j['job_type'].replace('_', ' '))}</td>"
                f"<td>{esc(j['status'])}</td>"
                f"<td>{j['credits_charged']} credits</td>"
                f"<td>{esc(j['result'] or '—')}</td></tr>"
                for j in _saas_store.list_jobs(account["id"])
            ) or "<tr><td colspan='4'>No jobs yet.</td></tr>"
            inner = f"""<p class='muted'>Track verified paying customers
from signed campaigns to checkout.</p>
<p class='muted'>Generated: {esc(utcnow().isoformat())}</p>
{_progress_grid_html(data)}
{_attribution_sections_html(data)}
<h2>Your websites</h2>
<div class="table-wrap"><table>
<tr><th>Website</th><th>Paying customers</th><th>Revenue</th></tr>
{site_rows}</table></div>
<h2>Recent jobs</h2>
<div class="table-wrap"><table>
<tr><th>Job</th><th>Status</th><th>Cost</th><th>Result</th></tr>
{job_rows}</table></div>"""
            return _operator_page("Acquisition & Attribution", inner,
                                  account=account, current="report",
                                  is_operator=False, extra_css=_REPORT_CSS)
        agent_rows = "".join(
            f"<tr><td>{esc(a['type'])}</td>"
            f"<td>{a['tasks_completed']}</td>"
            f"<td>{a['tokens_used']:,}</td>"
            f"<td>${a['cost_usd']:,.4f}</td>"
            f"<td>{esc(a['last_task']) or '—'}</td></tr>"
            for a in snap["agents"]
        ) or "<tr><td colspan='5'>No agent activity yet.</td></tr>"
        biz_rows = "".join(
            f"<tr><td>{esc(b['name'])}</td>"
            f"<td>{esc(b['business_type'])}</td>"
            f"<td>{esc(b['status'])}</td>"
            f"<td>{_money(b['revenue'])}</td>"
            f"<td>${b['total_costs']:,.2f}</td>"
            f"<td>{_money(b['net_profit'])}</td></tr>"
            for b in snap["businesses"]
        ) or "<tr><td colspan='6'>No businesses yet.</td></tr>"
        exp_block = (
            f"<h2>Experiment 001 — Evergreen Planners</h2>"
            f"<p>Verdict: <strong>{esc(exp['verdict'])}</strong> · "
            f"Day {exp['days_elapsed']}/60 ({exp['days_remaining']} remaining)</p>"
            f"<p>Orders: {exp['orders']} · Revenue: ${exp['revenue_usd']:,.2f} · "
            f"Spend: ${exp['spend_usd']:,.2f} · Pace: {exp['pace_pct']}%</p>"
        ) if exp else (
            "<h2>Experiment 001</h2><p>Not configured on this runtime.</p>"
        )
        # Net profit is None when any site's revenue is hidden — the
        # card reads "hidden" and the color stays neutral.
        net_color = ("var(--v-muted)" if fin["net_profit"] is None else
                     "var(--v-green)" if fin["net_profit"] >= 0 else
                     "var(--v-red)")
        inner = f"""<p class='muted'>Generated: {esc(utcnow().isoformat())} — Tick: {snap["tick"]}</p>
<h2>Financials</h2>
<div class="cards">
<div class="card"><div class="label">Revenue</div>
<div class="value">{_money(fin["revenue"])}</div></div>
<div class="card"><div class="label">Total costs</div>
<div class="value">{money(fin["total_costs"])}</div></div>
<div class="card"><div class="label">Net profit</div>
<div class="value" style="color:{net_color}">{_money(fin["net_profit"])}</div></div>
</div>
<h2>Agent performance</h2>
<div class="table-wrap"><table>
<tr><th>Agent</th><th>Tasks</th><th>Tokens</th><th>Cost (USD)</th>
<th>Last task</th></tr>{agent_rows}</table></div>
<h2>Business P&amp;L</h2>
<div class="table-wrap"><table>
<tr><th>Name</th><th>Type</th><th>Status</th><th>Revenue</th><th>Costs</th>
<th>Net profit</th></tr>{biz_rows}</table></div>
{exp_block}"""
        return _operator_page("Agent & Financial Report", inner,
                              account=account, current="report",
                              is_operator=True, extra_css=_REPORT_CSS)

    # -- the five platform sections (sidebar items) -----------------------
    # Each follows the /approvals handler as the template:
    # _need_account, per-account scoping, _operator_page with the
    # sidebar current key. All numbers are real; empty states are
    # honest; nothing here is demo data. The forced-2FA middleware
    # gates them automatically — they are NOT in _TOTP_FREE_PATHS.

    @app.get("/campaigns", response_class=HTMLResponse)
    def campaigns_page(request: Request):
        """Signed campaigns: one row per signed action — target URL,
        agent, verified conversions (attribution "ventoryn")."""
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        data = _attribution_data(account, check_visitors=False)
        sites = _saas_store.list_sites(account["id"])
        intro = ("<p class='muted'>Work that's live, and what it "
                 "earned. Campaigns start from a website — pick a "
                 "site, run agent work, approve the draft.</p>")
        if not sites:
            inner = (f"{intro}<div class='card'><p>No websites yet — add one "
                     "to run campaigns.</p><p><a class='btn btn-primary' "
                     "href='/account/dashboard'>Add a website</a></p>"
                     "</div>")
        elif not data["campaigns"]:
            inner = (f"{intro}<div class='card'><p>No signed campaigns yet — "
                     "run agent work from a site page.</p></div>")
        else:
            rows = "".join(
                f"<tr><td>{esc(c['url'])}</td><td>{esc(c['agent'])}</td>"
                f"<td>{c['verified']}</td><td>Active</td></tr>"
                for c in data["campaigns"])
            inner = f"""{intro}<div class="table-wrap"><table>
<tr><th>Campaign</th><th>Source</th><th>Verified conversions</th>
<th>Status</th></tr>{rows}</table></div>
<p class='muted'>Clicks per campaign are not tracked yet — the table
shows verified conversions only.</p>"""
        return _operator_page(
            "Campaigns", inner, account=account, current="campaigns",
            is_operator=_saas_config.is_operator(account["email"]),
            extra_css=_PORTFOLIO_CSS)


    def _start_campaign_card(account, started: str = "") -> str:
        """Agents: start an X campaign for a website chosen from a
        dropdown, with the cost estimated before anything runs."""
        from ventoryn.campaign_cost import (MAX_DAYS, X_RATE_LINKED_POST_USD,
                                            X_RATE_PLAIN_POST_USD, X_RATE_SOURCE)
        from ventoryn.saas import CREDIT_COSTS

        sites = _saas_store.list_sites(account["id"])
        if not sites:
            return ("<div class='card' id='start-campaign'><h2>Start a campaign</h2>"
                    "<p>Add a website first, then the agents can draft a campaign "
                    "for it.</p></div>")
        budget = _saas_config.x_monthly_budget_usd
        opts = "".join(
            f"<option value='{esc(s['id'])}' data-left="
            f"'{max(0.0, round(budget - _saas_store.x_spend_this_month_usd(s['id']), 2))}'>"
            f"{esc(s['host'] or s['url'])}</option>" for s in sites)
        note = ""
        if started:
            site = _saas_store.get_site(started)
            if site and site["account_id"] == account["id"]:
                note = (f"<p class='v-camp-started'>The agent is drafting a campaign for "
                        f"<strong>{esc(site['host'])}</strong>. It appears in "
                        f"<a href='/approvals'>Approvals</a> in a minute or two.</p>")
        recent = _saas_store.list_campaigns(account["id"], limit=8)
        hosts = {s["id"]: s["host"] for s in sites}
        status_text = {"proposed": "waiting for your approval", "scheduled": "approved — posting",
                       "rejected": "rejected", "done": "finished"}
        recent_html = "".join(
            f"<li><a href='/approvals#campaign-{esc(c['id'])}'>{esc(hosts.get(c['site_id'], ''))}</a>"
            f" — {c['days']} days — {esc(status_text.get(c['status'], c['status']))}"
            f"{' (autopilot)' if c['source'] == 'autopilot' else ''}</li>" for c in recent)
        blocker = site_jobs.server_blocker("x_campaign") if site_jobs else None
        return (
            f"<div class='card' id='start-campaign'><h2>Start a campaign</h2>{note}"
            "<p class='muted'>The agents draft the posts from your website's own pages. "
            "Nothing is posted until you approve the campaign in Approvals.</p>"
            + (f"<p class='v-camp-warn'>Unavailable on this server: {esc(blocker)}.</p>" if blocker else "")
            + f"<form method='post' action='/agents/campaigns' class='v-camp-form' id='camp-form' "
            f"data-plain='{X_RATE_PLAIN_POST_USD}' data-link='{X_RATE_LINKED_POST_USD}'>"
            f"<label>Website<select name='site_id' id='camp-site'>{opts}</select></label>"
            f"<label>Platform<select name='platform' disabled><option>X</option></select></label>"
            f"<label>Days<input type='number' name='days' id='camp-days' min='1' max='{MAX_DAYS}' value='30'></label>"
            f"<label>Posts a day<input type='number' name='posts_per_day' id='camp-ppd' min='1' max='2' step='1' value='1'></label>"
            f"<label class='v-camp-check'><input type='checkbox' name='with_link' value='1' id='camp-link' checked> "
            f"With a link (signed, so sales are counted)</label>"
            f"<p id='camp-est' class='v-camp-est'></p>"
            f"<p class='muted'>{esc(X_RATE_SOURCE)}. Drafting costs "
            f"{CREDIT_COSTS['x_campaign']} credits.</p>"
            f"<p><button class='btn btn-primary' type='submit'{' disabled' if blocker else ''}>"
            f"Start a campaign</button></p></form>"
            + (f"<h3>Recent campaigns</h3><ul>{recent_html}</ul>" if recent_html else "")
            + "<style>.v-camp-form{display:grid;grid-template-columns:repeat(auto-fit,"
            "minmax(170px,1fr));gap:12px 16px;align-items:end}.v-camp-form label{margin:0}"
            ".v-camp-form select,.v-camp-form input[type=number]{width:100%;margin-top:4px}"
            ".v-camp-form .v-camp-check,.v-camp-form p{grid-column:1/-1;margin:0}"
            ".v-camp-est{font-weight:600}.v-camp-warn{color:#92400e}"
            ".v-camp-started{background:#ecfdf5;border:1px solid #a7f3d0;padding:10px 12px;"
            "border-radius:10px}</style>"
            + "<script>(function(){var f=document.getElementById('camp-form');if(!f)return;"
            "var s=document.getElementById('camp-site'),d=document.getElementById('camp-days'),"
            "p=document.getElementById('camp-ppd'),l=document.getElementById('camp-link'),"
            "o=document.getElementById('camp-est');function m(n){return '$'+n.toFixed(2);}"
            "function run(){var days=Math.max(1,Math.floor(+d.value||1)),ppd=Math.max(1,Math.min(2,Math.floor(+p.value||1))),"
            "posts=Math.min(60,days*ppd),rate=+(l.checked?f.dataset.link:f.dataset.plain),"
            "total=Math.round(posts*rate*100)/100,"
            "left=+(s.options[s.selectedIndex].getAttribute('data-left')||0),"
            "fit=Math.floor(Math.round(left*1000)/Math.round(rate*1000));"
            "o.textContent='Estimated cost: '+posts+' X posts at $'+rate.toFixed(3)+' = about '+m(total)+"
            "(total<=left?' — fits this month\\u2019s X budget ('+m(left)+' left).':"
            "' — more than the '+m(left)+' left in this month\\u2019s X budget; it covers '+Math.min(posts,fit)+' posts.');}"
            "[s,d,p,l].forEach(function(e){e.addEventListener('input',run);e.addEventListener('change',run);});run();"
            "})();</script></div>")

    @app.post("/agents/campaigns")
    def start_campaign(request: Request, site_id: str = Form(...), days: str = Form("30"),
                       posts_per_day: str = Form("1"), with_link: str = Form("")):
        """Start the campaign agent for one website: charge the job,
        draft in the background, the proposal lands in Approvals."""
        import threading
        from ventoryn import autopilot as _auto
        from ventoryn.saas import CREDIT_COSTS, InsufficientCredits

        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = _saas_store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            raise HTTPException(404, "website not found")

        def _back(msg: str, code: int = 400):
            return HTMLResponse(_operator_page(
                "Agents", f"<div class='card'><p>{esc(msg)}</p><p><a href='/agents#start-campaign'>"
                f"Back to Agents</a></p></div>", account=account, current="agents",
                is_operator=_saas_config.is_operator(account["email"])), status_code=code)

        blocker = site_jobs.server_blocker("x_campaign")
        if blocker:
            return _back(f"Campaigns are unavailable on this server: {blocker}. Nothing was charged.", 503)
        try:
            d = max(1, min(int(days), 365))
            ppd = max(1, min(int(float(posts_per_day)), 2))
        except ValueError:
            return _back("Days and posts a day must be numbers. Nothing was charged.")
        is_op = _saas_config.is_operator(account["email"])
        try:
            job = _saas_store.charge_credits(account["id"], "x_campaign", site["id"],
                                             is_operator=is_op)
        except InsufficientCredits:
            return _back(f"Drafting a campaign costs {CREDIT_COSTS['x_campaign']} credits and "
                         f"the balance is {_saas_store.credit_balance(account['id'])}. "
                         "Nothing was charged.", 402)
        threading.Thread(
            target=_auto.execute, name="campaign-draft", daemon=True,
            args=(_saas_store, _saas_config, account, site, "x_campaign"),
            kwargs={"orchestrator": state["orchestrator"], "reports_dir": _reports_dir(),
                    "inputs": {"source": "manual", "days": d, "posts_per_day": ppd,
                               "with_link": "1" if with_link else "0"},
                    "job": job}).start()
        return RedirectResponse(f"/agents?campaign={site['id']}#start-campaign", status_code=303)

    def _reports_dir():
        from ventoryn.saas_routes import REPORTS_DIR
        return REPORTS_DIR

    def _approvals_campaigns_html(account) -> str:
        """Approvals: campaign proposals, scheduled campaigns, and the
        SEO article drafts the agents wrote."""
        from ventoryn import campaigns as _camp

        totp = getattr(app.state, "totp_field", lambda a: "")(account)
        proposed = _saas_store.list_campaigns(account["id"], status="proposed")
        scheduled = _saas_store.list_campaigns(account["id"], status="scheduled", limit=10)
        cards = "".join(_camp.proposal_card(_saas_store, _saas_config, c, totp)
                        for c in proposed)
        out = (f"<style>{_camp.CAMPAIGN_CSS}</style>"
               f"<h2>Campaign proposals ({len(proposed)})</h2>"
               + (cards or "<div class='card'><p class='muted'>No campaign waiting. Start one "
                  "from <a href='/agents#start-campaign'>Agents</a>, or let the autopilot "
                  "propose one each week.</p></div>"))
        if scheduled:
            out += ("<div class='card'><h2>Approved campaigns</h2><ul>"
                    + "".join(_camp.scheduled_summary(_saas_store, c) for c in scheduled)
                    + "</ul></div>")
        hosts = {s["id"]: s["host"] for s in _saas_store.list_sites(account["id"])}
        articles = [j for j in _saas_store.list_jobs(account["id"], limit=60)
                    if j["job_type"] == "seo_article" and j["status"] == "done"][:10]
        if articles:
            rows = "".join(
                f"<tr><td>{esc(hosts.get(j['site_id'], ''))}</td>"
                f"<td>{esc((j['result'] or '').replace('Automatic — ', '').split(' · report')[0])}</td>"
                f"<td>{esc(str(j['created_at'])[:10])}</td>"
                f"<td><a href='/account/sites/{esc(j['site_id'])}/articles/{esc(j['id'])}'>Read</a></td></tr>"
                for j in articles)
            out += ("<div class='card'><h2>SEO article drafts</h2><p class='muted'>Written by the "
                    "agents, never published. Read one, then copy it to your site if you like it.</p>"
                    "<div class='table-wrap'><table><tr><th>Website</th><th>Draft</th>"
                    f"<th>Date</th><th></th></tr>{rows}</table></div></div>")
        return out

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request, campaign: str = ""):
        """The real agents from the orchestrator registry, with live
        status from the audit log (agent_stats_from_audit): tasks
        completed, tokens, cost, last task. Display name + role come
        from the Live Ops crew cast; the registry description says
        what each agent does. Status is honest: "Last did: …" when
        the audit has a last run for the agent, "Standing by" when
        it has none — never a "Running" pill that cannot be proved.
        Display-only crew (Builder, Ledger) are not agent types and
        are not listed here."""
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        agent_types = state["orchestrator"].registry.agent_types()
        stats_list = (agent_stats_from_audit(state["audit"], agent_types)
                      if agent_types else [])
        if not agent_types:
            roster_html = ("<div class='card'><p>No agents registered "
                           "yet.</p></div>")
        else:
            stats = {s["type"]: s for s in stats_list}
            crew = {
                "discovery": ("Scout", "Discovery"),
                "research": ("Analyst", "Research"),
                "creative": ("Studio", "Creative"),
                "marketing": ("Growth", "Marketing"),
                "support": ("Care", "Support"),
                "operations": ("Ops", "Operations"),
                "review": ("Review", "Review"),
            }
            cards = ""
            for i, t in enumerate(agent_types):
                name, role = crew.get(
                    t, (t.replace("_", " ").title(), "Agent"))
                reg = state["orchestrator"].registry.get(t)
                desc = reg.description if reg else ""
                s = stats.get(t, {})
                last = s.get("last_task", "").replace(" · ", " — ")
                status = (f"Last did: {last}" if last
                          else "Standing by")
                cards += (
                    f"<article class='v-biz v-accent-{i % 4}'>"
                    f"<h3>{esc(name)} "
                    f"<span class='pill'>{esc(role)}</span></h3>"
                    f"<p class='v-biz-url'>{esc(desc)}</p>"
                    f"<p><strong>{esc(status)}</strong></p>"
                    f"<dl><dt>Tasks completed</dt>"
                    f"<dd>{s.get('tasks_completed', 0)}</dd>"
                    f"<dt>Tokens</dt>"
                    f"<dd>{s.get('tokens_used', 0):,}</dd>"
                    f"<dt>Cost</dt>"
                    f"<dd>${s.get('cost_usd', 0.0):,.4f}</dd></dl>"
                    f"</article>")
            roster_html = f"<div class='v-biz-grid'>{cards}</div>"
        # Moved here from Overview (agents live ONLY on this page):
        # "What the team is doing" (tasks & spend bars + activity
        # over time) and "What just happened" (the events feed).
        events = _activity_events(account, limit=200)
        feed_rows = ""
        for e in events[:30]:
            ids = " — ".join(x for x in (e.get("business_id"),
                                         e.get("task_id")) if x)
            meta = " — ".join(x for x in
                             (_fmt_event_time(e.get("ts", "")), ids)
                             if x)
            feed_rows += (
                f"<div class='v-feed-row'>"
                f"<span class='v-feed-voice'>{esc(_event_voice(e))}</span>"
                + (f"<span class='v-feed-meta'>{esc(meta)}</span>"
                   if meta else "")
                + "</div>")
        if not feed_rows:
            feed_rows = ("<p class='muted'>Quiet — for now. Your "
                         "websites' work will show up here.</p>")
        inner = (f"<p class='muted'>Agents research and draft. "
                 f"Nothing is published until you approve it on "
                 f"<a href='/approvals'>Approvals</a>.</p>"
                 f"{_start_campaign_card(account, campaign)}"
                 f"{roster_html}"
                 f"{_team_doing_html(stats_list, events)}"
                 f"<div class='card'><h2>What just happened</h2>"
                 f"{feed_rows}</div>")
        return _operator_page(
            "Agents", inner, account=account, current="agents",
            is_operator=_saas_config.is_operator(account["email"]),
            extra_css=_PORTFOLIO_CSS)

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request):
        """The funnel, per site, from real sources only: visitors
        from Plausible where connected, signups (no signup source
        exists yet — the page says so), paying customers + revenue
        from revenue_for_site respecting the privacy toggles, and
        the verified / pending / unattributed counts + 30-day
        verified trend from _attribution_data."""
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        from ventoryn.saas import plausible_visitors

        data = _attribution_data(account, check_visitors=False)
        counts = data["counts"]
        rows = ""
        from ventoryn.analytics import funnel_cells

        for s in data["sites"]:
            site = _saas_store.get_site(s["id"])
            visitors_html, signups_html = funnel_cells(
                _saas_store, site, plausible_visitors)
            rows += (
                f"<tr><td>{esc(s['url'])}</td><td>{visitors_html}</td>"
                f"<td>{signups_html}</td>"
                f"<td>{s['count_html']}</td>"
                f"<td>{s['revenue_html']}</td></tr>")
        rows = rows or ("<tr><td colspan='5'>No websites yet — add one "
                        "to see its funnel.</td></tr>")
        # Reports live here: the report page itself, plus (operator
        # only) the generated mission reports — view in the browser
        # or download the Markdown.
        if _saas_config.is_operator(account["email"]):
            from ventoryn.reports import list_reports

            report_rows = "".join(
                f"<tr><td>{esc(r['title'])}</td>"
                f"<td>{esc(r['date'] or r['generated_at'] or '—')}</td>"
                f"<td><a href='/reports/{esc(r['name'])}'>View</a></td>"
                f"<td><a href='/reports/{esc(r['name'])}/download'>"
                f"Download</a></td></tr>"
                for r in list_reports()) or (
                "<tr><td colspan='4'>No mission reports yet.</td></tr>")
            report_section = (f"<div class='card'><h2>Reports</h2>"
                "<p><a class='btn btn-primary' href='/report'>View the "
                "Agent &amp; Financial Report</a></p>"
                "<p class='muted'>The operator's agent + financial "
                "report, and the mission reports below — view in the "
                "browser or download the Markdown.</p>"
                "<div class='table-wrap'><table>"
                "<tr><th>Report</th><th>Date</th><th></th><th></th></tr>"
                f"{report_rows}</table></div></div>")
        else:
            report_section = (f"<div class='card'><h2>Reports</h2>"
                "<p><a class='btn btn-primary' href='/report'>View the "
                "Acquisition &amp; Attribution report</a></p>"
                "<p class='muted'>Progress, funnel, campaigns and jobs "
                "for this account.</p></div>")
        inner = f"""<div class='v-stats'>
<div class='v-stat v-stat-1'><span class='v-stat-label'>Verified</span>
<span class='v-stat-value'>{counts['verified']}</span></div>
<div class='v-stat v-stat-2'><span class='v-stat-label'>Pending</span>
<span class='v-stat-value'>{counts['pending']}</span></div>
<div class='v-stat v-stat-3'><span class='v-stat-label'>Unattributed</span>
<span class='v-stat-value'>{counts['unattributed']}</span></div></div>
<h2>Funnel — visitors to paying customers</h2>
<div class="table-wrap"><table>
<tr><th>Website</th><th>Visitors (30 days)</th><th>Signups</th>
<th>Paying customers</th><th>Revenue</th></tr>{rows}</table></div>
<p class='muted'>Every number here comes from a real source. Where a
connection is missing the value reads "unavailable" — never invented.
Signups read unavailable because no signup tracking is connected yet.</p>
<div class='card'><h2>Acquisition trend</h2>{data['trend_svg']}</div>
<div class='card'><h2>Conversion status</h2>
<div class='v-donut-wrap'>{data['donut_svg']}<ul class='v-legend'>{data['legend_html']}</ul></div></div>
{report_section}"""
        return _operator_page(
            "Analytics", inner, account=account, current="analytics",
            is_operator=_saas_config.is_operator(account["email"]),
            extra_css=_PORTFOLIO_CSS + _REPORT_CSS)

    # -- Integrations: apps -> websites -> a window per website ------------
    # /integrations is a grid of the service apps (X, Stripe sales,
    # Plausible, Reddit). Clicking an app opens /integrations/<app>: the
    # account's websites with their status for that app. Clicking a
    # website opens a window (<dialog>) with exactly what that app needs
    # for that website (API key, webhook secret, Connect with X...). The
    # forms post to the same endpoints as before; return_to brings the
    # owner back to the app page with the same website's window open.
    _INTEGRATION_APPS = (
        ("x", "X", "Post approved updates to each website's own X "
         "account. Nothing is posted until you approve it. X's API is "
         "paid and rate-limited.", "#0b0e1e"),
        ("facebook", "Facebook", "Post approved updates to each "
         "website's Facebook Page. Nothing is posted until you "
         "approve it.", "#1877f2"),
        ("instagram", "Instagram", "Post approved updates to each "
         "website's Instagram Business account. Nothing is posted "
         "until you approve it. Caption links are plain text, not "
         "clickable.", "#e1306c"),
        ("stripe_sales", "Stripe sales", "Count real paying customers "
         "from each website's own Stripe account.", "#635bff"),
        ("plausible", "Plausible", "Optional: bring visitor numbers from "
         "Plausible. Ventoryn Analytics already counts visitors without "
         "it.", "#5850ec"),
        ("reddit", "Reddit", "Not available: Reddit no longer offers the "
         "free API access this needs.", "#ff4500"),
    )
    _INTEGRATIONS_CSS = """
.v-apps { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 14px; margin: 16px 0; }
.v-app { display: flex; flex-direction: column; gap: 10px; padding: 18px 20px;
  background: var(--v-card); border: 1px solid var(--v-line);
  border-radius: var(--v-radius); box-shadow: var(--v-shadow); color: inherit;
  text-decoration: none; transition: border-color .15s, box-shadow .15s; }
a.v-app:hover { border-color: var(--v-violet-line); color: inherit;
  box-shadow: 0 6px 22px rgba(109, 40, 217, .12); }
.v-app-off { opacity: .7; }
.v-app-head { display: flex; align-items: center; gap: 12px; }
.v-app-mark { width: 40px; height: 40px; border-radius: 10px; color: #fff;
  display: inline-flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 18px; flex: 0 0 auto; }
.v-app-name { font-weight: 800; font-size: 17px; }
.v-app p { margin: 0; font-size: 14px; color: var(--v-muted); }
.v-site-list { list-style: none; padding: 0; margin: 12px 0; }
.v-site-row { display: flex; align-items: center; justify-content: space-between;
  gap: 12px; padding: 12px 16px; background: var(--v-card);
  border: 1px solid var(--v-line); border-radius: var(--v-radius-sm);
  margin-bottom: 8px; }
.v-site-row strong { overflow-wrap: anywhere; }
.v-site-row button { margin: 0; flex: 0 0 auto; }
dialog.v-dialog { border: 0; border-radius: 16px; padding: 0;
  width: min(560px, calc(100vw - 32px)); max-height: calc(100vh - 48px);
  box-shadow: 0 24px 60px rgba(11, 14, 30, .35); }
dialog.v-dialog::backdrop { background: rgba(11, 14, 30, .55); }
.v-dialog-head { display: flex; align-items: center; justify-content: space-between;
  gap: 12px; padding: 16px 20px; border-bottom: 1px solid var(--v-line);
  position: sticky; top: 0; background: var(--v-card); }
.v-dialog-head h2 { margin: 0; font-size: 18px; overflow-wrap: anywhere; }
.v-dialog-body { padding: 8px 20px 20px; overflow-y: auto; }
.v-dialog-close { margin: 0; min-height: 40px; }
.v-crumbs { font-size: 14px; margin: 0 0 6px; }
"""

    def _integration_status(site_id: str, kind: str) -> tuple[dict, bool]:
        conn = _saas_store.get_connection(site_id, kind) or {
            "kind": kind, "status": "not_connected", "note": ""}
        return conn, conn.get("status") == "connected"

    def _integration_body(account, s, kind: str, conn: dict,
                          return_to: str) -> str:
        """What one website needs for one app: the same forms as before
        (same endpoints), plus return_to so saving lands back here."""
        from ventoryn.saas import (CONNECTION_CONSENT, CONNECTION_SCOPES,
                                   check_posting_allowed, x_account_summary)

        rt = f"<input type='hidden' name='return_to' value='{esc(return_to)}'>"
        totp_field = ""
        if account.get("totp_enabled"):
            totp_field = (
                "<label>Two-factor code (fresh, required to confirm this "
                "change)</label><input type='text' name='totp_code' "
                "inputmode='numeric' autocomplete='one-time-code' maxlength='8'>")
        sid = esc(s["id"])
        connected = conn.get("status") == "connected"
        parts = []
        if conn.get("note"):
            parts.append(f"<p>{esc(conn['note'])}</p>")
        if kind in ("reddit", "x", "facebook", "instagram"):
            _ok, gate_reason = check_posting_allowed(
                _saas_store, account, s["id"], kind, _saas_config)
            parts.append(f"<p class='muted'>Posting: {esc(gate_reason)}</p>")
        if kind == "plausible":
            raw = (_saas_store.get_connection(s["id"], "plausible") or {}
                   ).get("_detail_raw") or {}
            parts.append(
                f"<form method='post' action='/account/sites/{sid}/connections/plausible'>{rt}"
                f"<label for='pl-dom-{sid}'>Plausible site domain</label>"
                f"<input type='text' id='pl-dom-{sid}' name='site_domain' "
                f"value='{esc(raw.get('site_domain', s['host']))}'>"
                f"<label for='pl-key-{sid}'>Plausible API key</label>"
                f"<input type='password' id='pl-key-{sid}' name='api_key' "
                f"autocomplete='off' placeholder='Stored encrypted, never shown back'>"
                f"<p><button class='btn btn-primary' type='submit'>"
                f"{'Update' if connected else 'Connect'} Plausible</button></p></form>")
        elif kind == "stripe_sales":
            webhook_url = f"{_saas_config.base_url}/account/billing/webhook"
            parts.append(
                f"<ol class='muted'><li>In this website's Stripe account, add a webhook "
                f"endpoint: <code>{esc(webhook_url)}</code></li>"
                f"<li>Add <code>ventoryn_site_id</code> = <code>{sid}</code> to the "
                f"checkout metadata, so sales are counted for this website.</li>"
                f"<li>Paste the endpoint's signing secret below.</li></ol>"
                f"<form method='post' action='/account/sites/{sid}/connections/stripe_sales'>{rt}"
                f"<label for='st-sec-{sid}'>Stripe webhook signing secret</label>"
                f"<input type='password' id='st-sec-{sid}' name='webhook_secret' "
                f"autocomplete='off' spellcheck='false' "
                f"placeholder='whsec_… (stored encrypted, never shown back)'>"
                f"<p><button class='btn btn-primary' type='submit'>"
                f"{'Update' if connected else 'Connect'} Stripe sales</button></p></form>")
        elif kind == "x":
            if connected:
                token = (conn.get("_detail_raw") or (
                    _saas_store.get_connection(s["id"], "x") or {}).get(
                        "_detail_raw") or {}).get("access_token", "")
                summary = x_account_summary(token) if token else None
                parts.append(
                    f"<p class='muted'>Audience: {summary['followers']} followers, "
                    f"{summary['following']} following, {summary['posts']} posts.</p>"
                    if summary else
                    "<p class='muted'>Audience numbers unavailable right now.</p>")
                parts.append(
                    f"<p><a class='btn' href='/account/sites/{sid}#connections'>"
                    f"Drafts and posting for this website</a></p>")
            elif _saas_config.x_oauth_configured and _saas_config.app_registered("x"):
                parts.append(
                    f"<p class='muted'>You will sign in to X and approve Ventoryn's "
                    f"access for this website's account, then come back here.</p>"
                    f"<p><a class='btn btn-primary' href='/account/sites/{sid}"
                    f"/connections/x/start?return_to="
                    f"{esc(urllib.parse.quote(return_to, safe=''))}'>Connect with X</a></p>")
            else:
                parts.append(
                    "<p class='muted'>The operator has not set up Ventoryn's X app "
                    "yet, so this website cannot connect. Nothing is faked.</p>")
        elif kind == "facebook":
            if connected:
                page_name = ((_saas_store.get_connection(
                    s["id"], "facebook") or {}).get("_detail_raw") or {}
                    ).get("page_name", "")
                identity = (f"Page: {page_name}" if page_name
                            else "Connected — Facebook Page.")
                parts.append(f"<p class='muted'>{esc(identity)}</p>")
                parts.append(
                    f"<p><a class='btn' href='/account/sites/{sid}#connections'>"
                    f"Drafts and posting for this website</a></p>")
            elif _saas_config.facebook_configured and \
                    _saas_config.app_registered("facebook"):
                parts.append(
                    f"<p class='muted'>You will sign in to Facebook and "
                    f"approve Ventoryn's access for this website's Page, "
                    f"then come back here.</p>"
                    f"<p><a class='btn btn-primary' href='/account/sites/{sid}"
                    f"/connections/facebook/start'>Connect with Facebook</a></p>")
            else:
                parts.append(
                    "<p class='muted'>The operator has not set up Ventoryn's "
                    "Meta app yet, so this website cannot connect. Nothing "
                    "is faked.</p>")
        elif kind == "instagram":
            if connected:
                parts.append(
                    "<p class='muted'>Connected — Instagram Business "
                    "account.</p>")
                parts.append(
                    f"<p><a class='btn' href='/account/sites/{sid}#connections'>"
                    f"Drafts and posting for this website</a></p>")
            elif _saas_config.facebook_configured and \
                    _saas_config.app_registered("instagram"):
                parts.append(
                    f"<p class='muted'>You will sign in to Facebook and "
                    f"approve Ventoryn's access for this website's "
                    f"Instagram Business account, then come back here.</p>"
                    f"<p><a class='btn btn-primary' href='/account/sites/{sid}"
                    f"/connections/instagram/start'>Connect with Instagram</a></p>")
            else:
                parts.append(
                    "<p class='muted'>The operator has not set up Ventoryn's "
                    "Meta app yet, so this website cannot connect. Nothing "
                    "is faked.</p>")
        else:
            parts.append("<p class='muted'>Reddit no longer offers the free API "
                         "access this would need, so this connection is not "
                         "available.</p>")
        consent = CONNECTION_CONSENT.get(kind, "")
        scopes = CONNECTION_SCOPES.get(kind, "")
        if consent:
            parts.append(f"<p class='muted'>{esc(consent)}</p>")
        if scopes:
            parts.append(f"<p class='muted'>Access requested: <code>{esc(scopes)}</code></p>")
        if connected:
            parts.append(
                f"<form method='post' action='/account/sites/{sid}/connections/"
                f"{esc(kind)}/disconnect'>{rt}{totp_field}<p><button class='btn' "
                f"type='submit'>Disconnect</button></p></form>")
        return "".join(parts)

    @app.get("/integrations", response_class=HTMLResponse)
    def integrations_page(request: Request):
        """The service apps, each with how many websites use it."""
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        sites = _saas_store.list_sites(account["id"])
        cards = ""
        for kind, name, blurb, color in _INTEGRATION_APPS:
            n = sum(1 for s in sites if _integration_status(s["id"], kind)[1])
            if kind == "reddit":
                status = "<span class='badge badge-off'>Not available</span>"
            elif not sites:
                status = "<span class='badge badge-off'>No websites yet</span>"
            else:
                status = (f"<span class='badge{' badge-ok' if n else ' badge-off'}'>"
                          f"Connected on {n} of {len(sites)} website"
                          f"{'s' if len(sites) != 1 else ''}</span>")
            head = (f"<div class='v-app-head'><span class='v-app-mark' "
                    f"style='background:{color}' aria-hidden='true'>{esc(name[0])}"
                    f"</span><span class='v-app-name'>{esc(name)}</span></div>")
            if kind == "reddit":
                cards += (f"<div class='v-app v-app-off'>{head}<p>{esc(blurb)}</p>"
                          f"<div>{status}</div></div>")
            else:
                cards += (f"<a class='v-app' href='/integrations/{kind}'>{head}"
                          f"<p>{esc(blurb)}</p><div>{status}</div></a>")
        # Recommended next: without Stripe sales the funnel cannot verify
        # paying customers — say so plainly, linking to that site's window.
        hints = "".join(
            f"<div class='v-notice v-notice-warn'>"
            f"<a href='/integrations/stripe_sales?site={esc(s['id'])}'>"
            f"Connect Stripe sales for {esc(s['host'])}</a> to count paying "
            f"customers — the funnel cannot verify sales without it.</div>"
            for s in sites if not _integration_status(s["id"], "stripe_sales")[1])
        inner = (f"{hints}<p class='muted'>Choose an app to see your websites "
                 f"and connect each one.</p><div class='v-apps'>{cards}</div>")
        return _operator_page(
            "Integrations", inner, account=account, current="integrations",
            is_operator=_saas_config.is_operator(account["email"]),
            extra_css=_PORTFOLIO_CSS + _INTEGRATIONS_CSS)

    @app.get("/integrations/{kind}", response_class=HTMLResponse)
    def integration_app_page(request: Request, kind: str, site: str = ""):
        """One app: the account's websites, each opening its own window."""
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        meta = next((a for a in _INTEGRATION_APPS if a[0] == kind), None)
        if meta is None or kind == "reddit":
            raise HTTPException(404, "integration not found")
        _k, name, blurb, color = meta
        sites = _saas_store.list_sites(account["id"])
        rows, dialogs = "", ""
        for s in sites:
            conn, connected = _integration_status(s["id"], kind)
            sid = esc(s["id"])
            badge = ("<span class='badge badge-ok'>Connected</span>" if connected
                     else "<span class='badge badge-off'>Not connected</span>")
            rows += (f"<li class='v-site-row'><div><strong>{esc(s['host'])}</strong> "
                     f"{badge}</div><button class='btn' type='button' "
                     f"data-open='dlg-{sid}'>{'Manage' if connected else 'Connect'}"
                     f"</button></li>")
            body = _integration_body(account, s, kind, conn,
                                     f"/integrations/{kind}?site={s['id']}")
            dialogs += (f"<dialog class='v-dialog' id='dlg-{sid}' aria-labelledby='dlgt-{sid}'>"
                        f"<div class='v-dialog-head'><h2 id='dlgt-{sid}'>{esc(name)} for "
                        f"{esc(s['host'])}</h2><button class='btn v-dialog-close' "
                        f"type='button' data-close>Close</button></div>"
                        f"<div class='v-dialog-body'>{body}</div></dialog>")
        if not sites:
            rows = ("<li class='v-site-row'><span>No websites yet.</span>"
                    "<a class='btn btn-primary' href='/account/sites/new'>"
                    "Add a website</a></li>")
        open_id = f"dlg-{site}" if any(s["id"] == site for s in sites) else ""
        script = ("<script>(function(){"
                  "document.querySelectorAll('[data-open]').forEach(function(b){"
                  "b.addEventListener('click',function(){var d=document.getElementById("
                  "b.getAttribute('data-open'));if(d&&d.showModal)d.showModal();});});"
                  "document.querySelectorAll('dialog.v-dialog').forEach(function(d){"
                  "d.querySelectorAll('[data-close]').forEach(function(c){"
                  "c.addEventListener('click',function(){d.close();});});"
                  "d.addEventListener('click',function(e){if(e.target===d)d.close();});});"
                  + (f"var o=document.getElementById('{esc(open_id)}');"
                     f"if(o&&o.showModal)o.showModal();" if open_id else "")
                  + "})();</script>")
        inner = (f"<p class='v-crumbs'><a href='/integrations'>Integrations</a></p>"
                 f"<div class='v-app-head' style='margin:4px 0 8px'><span class='v-app-mark' "
                 f"style='background:{color}' aria-hidden='true'>{esc(name[0])}</span>"
                 f"<span class='v-app-name'>{esc(name)}</span></div>"
                 f"<p class='muted'>{esc(blurb)}</p>"
                 f"<h2>Your websites</h2><ul class='v-site-list'>{rows}</ul>"
                 f"{dialogs}{script}")
        return _operator_page(
            name, inner, account=account, current="integrations",
            is_operator=_saas_config.is_operator(account["email"]),
            extra_css=_PORTFOLIO_CSS + _INTEGRATIONS_CSS)

    @app.get("/customers", response_class=HTMLResponse)
    def customers_page(request: Request):
        """Real conversion records per site, with status verified /
        pending / unattributed — the privacy toggles respected
        exactly as /report does: a site with the count hidden
        contributes nothing to the displayed counts; details are
        hidden unless details_show is on; revenue is hidden unless
        revenue_show is on (amounts are stripped by
        revenue_for_site, so they cannot leak through the table)."""
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        sites = _saas_store.list_sites(account["id"])
        counted_ids = [s["id"] for s in sites
                       if s.get("count_show", 1)]
        counts = _saas_store.conversion_status_counts(counted_ids)
        rows = ""
        for s in sites:
            rev = _saas_store.revenue_for_site(s["id"])
            if rev is None:
                continue
            if rev["conversions"]:
                for c in rev["conversions"]:
                    if c.get("refunded_at"):
                        label = "Refunded"
                    elif c.get("disputed_at"):
                        label = "Disputed"
                    elif c.get("attribution") == "ventoryn":
                        label = "Verified"
                    elif c.get("source") == "manual":
                        label = "Pending"
                    else:
                        label = "Unattributed"
                    amount = (
                        f"${c['amount_cents'] / 100:,.2f} "
                        f"{esc(c['currency'].upper())}"
                        if c.get("amount_cents") is not None else
                        "<span class='unavailable'>hidden</span>")
                    # The signature tag links to its campaign where
                    # one exists — the tag is the proof of which
                    # campaign earned this customer.
                    tag = c.get("signature_tag") or "—"
                    if c.get("action_id"):
                        tag_html = (f"<a href='/campaigns'>"
                                    f"{esc(tag)}</a>")
                    else:
                        tag_html = esc(tag)
                    rows += (
                        f"<tr><td>{esc(s['url'])}</td><td>{amount}</td>"
                        f"<td>{esc(c['source'])}</td><td>{label}</td>"
                        f"<td>{tag_html}</td>"
                        f"<td>{esc(c['created_at'][:10])}</td></tr>")
            elif not s.get("details_show", 1):
                rows += (
                    f"<tr><td>{esc(s['url'])}</td>"
                    f"<td colspan='5'>Conversion details are hidden — "
                    f"turn them on in Data &amp; privacy controls.</td>"
                    f"</tr>")
        rows = rows or ("<tr><td colspan='6'>No paying customers "
                        "recorded yet.</td></tr>")
        inner = f"""<div class='v-stats'>
<div class='v-stat v-stat-1'><span class='v-stat-label'>Verified</span>
<span class='v-stat-value'>{counts['verified']}</span></div>
<div class='v-stat v-stat-2'><span class='v-stat-label'>Pending</span>
<span class='v-stat-value'>{counts['pending']}</span></div>
<div class='v-stat v-stat-3'><span class='v-stat-label'>Unattributed</span>
<span class='v-stat-value'>{counts['unattributed']}</span></div></div>
<div class="table-wrap"><table>
<tr><th>Website</th><th>Amount</th><th>Source</th><th>Status</th>
<th>Signature</th><th>Date</th></tr>{rows}</table></div>
<p class='muted'>Verified — the Ventoryn signature reached the payment.
Pending — owner-recorded, not yet verified by Stripe. Unattributed —
a real Stripe payment, no signature.</p>"""
        return _operator_page(
            "Customers", inner, account=account, current="customers",
            is_operator=_saas_config.is_operator(account["email"]),
            extra_css=_PORTFOLIO_CSS)

    @app.post("/api/tick")
    def api_tick(request: Request):
        """One orchestrator tick. Operator only — a customer must
        never be able to drive the simulation (or reset it).
        Refused while the operator-global pause is on: a paused
        tick does not run and does not advance the counter."""
        _, redirect = _need_operator(request)
        if redirect is not None:
            return JSONResponse({"ok": False, "error": "operator only"},
                                status_code=403)
        if _saas_store.get_global_pause():
            return JSONResponse(
                {"ok": False,
                 "error": "paused — resume all agents to run ticks"},
                status_code=409)
        from ecosystem.worker import run_etsy_monitor_tick, run_tick

        game["tick"] += 1
        tick = game["tick"]
        try:
            result = run_tick(_rt(), tick)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc),
                                 "snapshot": _snapshot()})
        if result.get("ok"):
            state["audit"].record(
                agent_type="game", event="tick_completed",
                result={"tick": tick, "winner": result.get("winner"),
                        "score": result.get("score")})
        if tick % 10 == 0:
            snap = run_etsy_monitor_tick(_rt(), tick)
            if snap:
                game["experiment_001"] = snap
        return JSONResponse({"ok": result.get("ok", False),
                             "snapshot": _snapshot()})

    @app.post("/api/pause")
    def api_pause(request: Request, paused: str = Form("0"),
                  next: str = Form("/ops")):
        """Operator only: pause / resume ALL agents. The flag is
        persisted in the state DB and gates the orchestrator tick
        loop (/api/tick refuses while paused; the worker loop skips
        tick work). On-demand analyze jobs are untouched. A form
        POST (the Overview button + the Settings control) that
        redirects back to `next` — a same-site path only."""
        _, redirect = _need_operator(request)
        if redirect is not None:
            return JSONResponse({"ok": False, "error": "operator only"},
                                status_code=403)
        _saas_store.set_global_pause(paused == "1")
        dest = (next if (next.startswith("/")
                         and not next.startswith("//")) else "/ops")
        return RedirectResponse(dest, status_code=303)

    @app.post("/api/reset")
    def api_reset(request: Request):
        """Operator only — see /api/tick."""
        _, redirect = _need_operator(request)
        if redirect is not None:
            return JSONResponse({"ok": False, "error": "operator only"},
                                status_code=403)
        from ecosystem.runtime import build_runtime

        use_pg = bool(os.environ.get("DATABASE_URL"))
        rt = build_runtime(use_postgres=use_pg, approvals_persist=True)
        app.state.runtime_holder["rt"] = rt
        state["orchestrator"] = rt.orchestrator
        state["registry"] = rt.businesses
        state["ledger"] = rt.ledger
        state["experiments"] = rt.experiments
        state["approvals"] = rt.approvals
        state["audit"] = rt.audit
        game["tick"] = 0
        game["experiment_001"] = None
        return JSONResponse({"ok": True, "snapshot": _snapshot()})

    # Holds the live Runtime so /api/tick and /api/reset share it with the
    # dashboard. ecosystem.runtime.create_dashboard_app stashes the real one;
    # direct create_app() usage falls back to a fresh runtime here.
    if not app.state.runtime_holder:
        from ecosystem.runtime import build_runtime as _br

        app.state.runtime_holder = {"rt": _br(
            use_postgres=bool(os.environ.get("DATABASE_URL")))}

    def _rt():
        return app.state.runtime_holder["rt"]

    # -- /admin: operator oversight, separate URL --------------------------
    # ventoryn.app/admin is the operator home — and ONLY this: who
    # is live now, who subscribed, all accounts, and what the agents
    # are doing (activity, health, problems). Aggregates only: the
    # admin does NOT get any customer's portfolio or approvals —
    # those are the customer's own financial decisions, made alone.
    # No approve buttons for customer items anywhere here, and no
    # per-customer site URLs or funnels.
    #
    # The gate is a PASSWORD, not the Google session:
    # VENTORYN_ADMIN_PASSWORD (env, Spencer's value, never in chat
    # or the repo), checked server-side only. A signed-in Google
    # session alone never opens /admin — not even the operator's.
    # Unset password => login always fails with the same generic
    # message (fail closed). Same pattern as the Kay Ayiti and Road
    # to CISSP admins.
    def _need_admin(request: Request):
        """(True, None) or (None, redirect to /admin/login) — the
        admin password session is what opens /admin, never a Google
        session."""
        token = request.cookies.get(ADMIN_SESSION_COOKIE, "")
        if not _saas_store.check_admin_session(token):
            return None, RedirectResponse("/admin/login", status_code=303)
        session = _saas_store.get_admin_session(token)
        if _saas_store.admin_totp_state()["enabled"] and not (
                session or {}).get("totp_verified"):
            return None, RedirectResponse("/admin/login", status_code=303)
        return True, None

    def _admin_page(title: str, inner: str, current: str) -> str:
        """Admin shell: the platform shell (purple sidebar with the
        Admin group active + app header), keeping only the board
        CONTENT styling (_ADMIN_CSS cards/tables) as extra_css.
        The admin pages open on the admin password session, never a
        Google session, so the sidebar's Growth Plan card + the
        Approvals badge read the operator account from the store
        (verified over their counted sites); when the operator has
        no Google account yet the card reads 0 and the Admin group
        still shows — the admin pages are operator-only either
        way."""
        from ventoryn.nav import platform_head_html, sidebar_html

        operator_email = (_saas_config.operator_email or "").strip().lower()
        operator = None
        for account in _saas_store.list_accounts():
            if account.get("email", "").strip().lower() == operator_email:
                operator = account
                break
        verified = 0
        pending = 0
        if operator is not None:
            sites = _saas_store.list_sites(operator["id"])
            counted_ids = [s["id"] for s in sites
                           if s.get("count_show", 1)]
            verified = _saas_store.conversion_status_counts(
                counted_ids)["verified"]
            owned = _owned_business_ids(operator)
            pending = len([a for a in state["approvals"].pending()
                           if _approval_visible(operator, a, owned)])
            # Social drafts awaiting the operator's approval count
            # toward the same Approvals badge — one number, one page.
            pending += sum(
                1 for s in sites
                for d in _saas_store.list_social_drafts(s["id"])
                if d.get("status") == "pending")
        sidebar = sidebar_html(
            current, True, verified=verified, pending_approvals=pending,
            account_email=(operator or {}).get("email", ""))
        head = platform_head_html(title, pending_approvals=pending)
        return ui.page(f"{title} (admin)", inner +
                       "<p class='v-admin-foot'>Signed in as admin · "
                       "<a href='/admin/logout'>Admin sign out</a></p>",
                       sidebar=sidebar, page_head=head,
                       extra_css=_ADMIN_CSS, body_class="v-admin")

    def _admin_auth_page(title: str, inner: str) -> str:
        """Password-only login page: no header, no menu, no Google
        button — just the password field."""
        return ui.page(f"{title} (admin)",
                       f"<div class='v-auth-card'>"
                       f"<div class='v-auth-brand'>Ventoryn</div>"
                       f"<h1>{esc(title)}</h1>{inner}</div>",
                       extra_css=_ADMIN_CSS, body_class="v-admin")

    @app.get("/admin/login", response_class=HTMLResponse)
    def admin_login_page(request: Request, error: str = ""):
        if _saas_store.check_admin_session(
                request.cookies.get(ADMIN_SESSION_COOKIE, "")):
            return RedirectResponse("/admin", status_code=303)
        totp_on = _saas_store.admin_totp_state()["enabled"]
        code_hint = (
            "<p class='muted'>Two-factor is enabled: enter the 6-digit "
            "code from your authenticator app (or a backup code) as "
            "well.</p>" if totp_on else
            "<p class='muted'>Two-factor is not enabled yet — the code "
            "field can stay empty. Set it up under /admin once you are "
            "in.</p>")
        inner = ("<p>Enter the admin password to open the Ventoryn "
                 "admin.</p>"
                 + ("<p class='v-error'>Incorrect admin password or "
                    "code.</p>" if error else "")
                 + "<form method='post' action='/admin/login'>"
                   "<label for='v-admin-password'>Admin password</label>"
                   "<input id='v-admin-password' name='password' "
                   "type='password' autocomplete='current-password' "
                   "required>"
                   "<label for='v-admin-code'>Authenticator code</label>"
                   "<input id='v-admin-code' name='code' type='text' "
                   "inputmode='numeric' autocomplete='one-time-code' "
                   "maxlength='8' placeholder='6-digit code, if enabled'>"
                 + code_hint +
                   "<button type='submit'>Open admin</button></form>")
        return HTMLResponse(_admin_auth_page("Admin sign-in", inner))

    @app.post("/admin/login")
    def admin_login(request: Request, password: str = Form(""),
                    code: str = Form("")):
        # Fail closed: an unset password fails exactly like a wrong
        # one — the same generic message either way. Constant-time
        # compare; the password and the code are never logged or
        # rendered. Wrong guesses are rate-limited (lockout after a few
        # failures) and every attempt is recorded in the audit log.
        key = request.client.host if request.client else "unknown"
        if _saas_store.admin_login_locked(key):
            state["audit"].record(agent_type="admin",
                                 event="admin_login",
                                 result={"success": False, "locked": True})
            return RedirectResponse("/admin/login?error=1",
                                    status_code=303)
        ok = (_saas_config.admin_configured and hmac.compare_digest(
            password, _saas_config.admin_password))
        totp_on = _saas_store.admin_totp_state()["enabled"]
        if ok and totp_on:
            c = (code or "").strip()
            ok = bool(c) and (
                _saas_store.verify_admin_totp_code(c)
                or _saas_store.consume_admin_backup_code(c))
        _saas_store.record_admin_attempt(key, bool(ok))
        state["audit"].record(agent_type="admin", event="admin_login",
                             result={"success": bool(ok),
                                     "totp_required": totp_on})
        if not ok:
            return RedirectResponse("/admin/login?error=1",
                                    status_code=303)
        token = _saas_store.create_admin_session()
        if totp_on:
            _saas_store.mark_admin_session_totp_verified(token)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(ADMIN_SESSION_COOKIE, token, httponly=True,
                            samesite="lax", path="/",
                            secure=_saas_config.cookie_secure,
                            max_age=ADMIN_SESSION_TTL_HOURS * 3600)
        return response

    @app.get("/admin/logout")
    def admin_logout(request: Request):
        _saas_store.delete_admin_session(
            request.cookies.get(ADMIN_SESSION_COOKIE, ""))
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
        return response

    # -- admin two-factor ---------------------------------------
    # Setup lives under /admin, after the password login. The secret is
    # generated here, shown ONCE as a QR code + manual key, and only
    # confirmed by a code — until then password-only login still works,
    # so the build itself can never lock the operator out. Once enabled,
    # the code is required at every login (_need_admin enforces it).
    @app.get("/admin/two-factor/setup", response_class=HTMLResponse)
    def admin_totp_setup_page(request: Request, error: str = ""):
        _, redirect = _need_admin(request)
        if redirect is not None:
            return redirect
        state2 = _saas_store.admin_totp_state()
        if state2["enabled"]:
            inner = ("<div class='v-admin-card'><h2>Two-factor is on</h2>"
                     "<p>The admin login requires the password and a "
                     "6-digit authenticator code at every sign-in.</p>"
                     "<form method='post' action='/admin/two-factor/reset'>"
                     "<button type='submit'>Reset two-factor</button></form>"
                     "<p class='muted'>Resetting turns two-factor off — "
                     "the next admin login is password-only until it is "
                     "set up again.</p></div>")
            return _admin_page("Two-factor", inner, "admin-live")
        secret = _saas_store.get_admin_totp_pending_secret()
        if not secret:
            secret = _saas_store.begin_admin_totp_setup()["secret"]
        uri = provisioning_uri(secret, "admin@ventoryn.app",
                               issuer="Ventoryn")
        inner = ("<div class='v-admin-card'><h2>Set up two-factor</h2>"
                 "<p>Two-factor is required for the admin once it is "
                 "enabled. Scan this QR code with your authenticator "
                 "app, then enter the 6-digit code it shows.</p>"
                 + ("<p class='v-error'>That code did not match — try "
                    "again.</p>" if error else "")
                 + f"<div class='qr'>{qr_svg(uri)}</div>"
                 + f"<p class='muted'>Or enter this key manually: "
                   f"<code>{esc(secret)}</code></p>"
                 + "<form method='post' action='/admin/two-factor/setup'>"
                   "<label for='v-totp-code'>6-digit code</label>"
                   "<input id='v-totp-code' name='code' type='text' "
                   "inputmode='numeric' autocomplete='one-time-code' "
                   "required maxlength='6'>"
                   "<p><button type='submit'>Confirm and turn on "
                   "two-factor</button></p></form>"
                 + "<p class='muted'>Backup codes are shown once after "
                   "setup — save them. If you lose your phone and the "
                   "codes, two-factor can only be reset from inside an "
                   "admin session.</p></div>")
        return _admin_page("Two-factor", inner, "admin-live")

    @app.post("/admin/two-factor/setup", response_class=HTMLResponse)
    def admin_totp_setup_confirm(request: Request, code: str = Form("")):
        _, redirect = _need_admin(request)
        if redirect is not None:
            return redirect
        if _saas_store.confirm_admin_totp_setup(code.strip()):
            codes = _saas_store.regenerate_admin_backup_codes()
            codes_html = "".join(
                f"<div>{esc(c[:4])} {esc(c[4:])}</div>" for c in codes)
            inner = ("<div class='v-admin-card'><h2>Two-factor is on</h2>"
                     "<p>Save these backup codes now — each works once, "
                     "and they are the way back in if you lose your "
                     "phone. They are shown only this once.</p>"
                     f"<div class='codes'>{codes_html}</div>"
                     "<p><a class='btn' href='/admin'>Back to the "
                     "admin</a></p></div>")
            state["audit"].record(agent_type="admin",
                                 event="admin_totp_enabled",
                                 result={"success": True})
            return _admin_page("Two-factor", inner, "admin-live")
        state["audit"].record(agent_type="admin",
                             event="admin_totp_enabled",
                             result={"success": False})
        return RedirectResponse("/admin/two-factor/setup?error=1",
                                status_code=303)

    @app.post("/admin/two-factor/reset")
    def admin_totp_reset(request: Request):
        _, redirect = _need_admin(request)
        if redirect is not None:
            return redirect
        _saas_store.reset_admin_totp()
        state["audit"].record(agent_type="admin",
                             event="admin_totp_reset",
                             result={"success": True})
        return RedirectResponse("/admin/two-factor/setup", status_code=303)

    @app.post("/admin/accounts/{account_id}/reset-totp")
    def admin_reset_customer_totp(request: Request, account_id: str):
        _, redirect = _need_admin(request)
        if redirect is not None:
            return redirect
        account = _saas_store.get_account(account_id)
        if account is None:
            return RedirectResponse("/admin/accounts", status_code=303)
        _saas_store.reset_totp(account_id)
        state["audit"].record(
            agent_type="admin", event="admin_customer_totp_reset",
            result={"success": True, "email": account["email"]})
        return RedirectResponse("/admin/accounts", status_code=303)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request):
        _, redirect = _need_admin(request)
        if redirect is not None:
            return redirect
        # Stamp the viewer as live before rendering, so the operator
        # sees himself in the list on his first load (the request
        # middleware stamps after the response, which would be one
        # page behind).
        account = _current_account(request)
        if account is not None:
            _saas_store.touch_activity(account["id"], "Admin")
        live = _saas_store.live_accounts()
        events = _saas_store.recent_activity_events(limit=30)
        accounts = _saas_store.list_accounts()
        subscribers = [a for a in accounts
                       if a["subscription_status"] == "active"]
        live_rows = "".join(
            f"<tr><td><span class='v-live-dot'></span>"
            f"{esc(a['email'])}</td>"
            f"<td>{esc(a['name'] or '—')}</td>"
            f"<td>{esc(a['last_action'] or '—')}</td>"
            f"<td>{_ago(a['last_seen_at'])}</td></tr>"
            for a in live) or (
            "<tr><td colspan='4'>Nobody is on the platform right "
            "now.</td></tr>")
        event_rows = "".join(
            f"<tr><td>{esc((e['created_at'] or '')[:19].replace('T', ' '))}</td>"
            f"<td>{esc(e['email'])}</td>"
            f"<td>{esc(_ACTIVITY_KIND_LABELS.get(e['kind'], e['kind']))}</td>"
            f"<td>{esc(e['detail'] or '—')}</td></tr>"
            for e in events) or (
            "<tr><td colspan='4'>No activity recorded yet.</td></tr>")
        totals = _saas_store.admin_revenue_totals()
        inner = (f"<div class='v-admin-card'><h2>Live now "
                 f"<span class='v-count'>({len(live)})</span></h2>"
                 f"<p class='muted'>Accounts active in the last "
                 f"{LIVE_WINDOW_MINUTES} minutes — stamped when a "
                 f"signed-in account makes a request. Nobody is "
                 f"invented; stale sessions drop off.</p>"
                 f"<div class='table-wrap'><table>"
                 f"<tr><th>Account</th><th>Name</th><th>Last action</th>"
                 f"<th>Last seen</th></tr>{live_rows}</table></div></div>"
                 f"<div class='v-admin-card'><h2>Recent activity</h2>"
                 f"<p class='muted'>Sign-ins, websites added, "
                 f"subscriptions, analyze runs — only real events.</p>"
                 f"<div class='table-wrap'><table>"
                 f"<tr><th>When</th><th>Account</th><th>Event</th>"
                 f"<th>Detail</th></tr>{event_rows}</table></div></div>"
                 f"<div class='v-admin-card'><h2>At a glance</h2>"
                 f"<p>Subscribers: <strong>{len(subscribers)}</strong> · "
                 f"Accounts: <strong>{len(accounts)}</strong></p>"
                 f"<p>Platform revenue (sites with revenue display on): "
                 f"<strong>${totals['total_cents'] / 100:,.2f} "
                 f"{totals['currency'].upper()}</strong> · Paying customers "
                 f"(count display on): <strong>{totals['paying_customers']}</strong></p>"
                 f"<p class='muted'>Dollar figures include only sites whose "
                 f"revenue display is on; counts include only sites whose count "
                 f"display is on.</p>"
                 f"<p><a href='/admin/two-factor/setup'>Admin two-factor "
                 f"setup</a></p></div>")
        return _admin_page("Live", inner, "admin-live")

    @app.get("/admin/subscribers", response_class=HTMLResponse)
    def admin_subscribers(request: Request):
        _, redirect = _need_admin(request)
        if redirect is not None:
            return redirect
        rows = ""
        for account in _saas_store.list_accounts():
            if account["subscription_status"] != "active":
                continue
            sites = _saas_store.list_sites(account["id"])
            paying = sum(
                _saas_store.paying_customer_count(s["id"]) for s in sites)
            rows += (
                f"<tr><td>{esc(account['email'])}</td>"
                f"<td>{esc(account['name'] or '—')}</td>"
                f"<td>{len(sites)}</td><td>{paying}</td>"
                f"<td>{'Unlimited' if _saas_config.is_operator(account['email']) else account['credits_balance']}</td></tr>")
        inner = ("<div class='v-admin-card'>"
                 "<p class='muted'>Every account with an active "
                 "$20/month subscription. Counts only — no site URLs, "
                 "no funnels, no approvals: those belong to the "
                 "customer.</p>"
                 "<div class='table-wrap'><table>"
                 "<tr><th>Email</th><th>Name</th><th>Sites</th>"
                 "<th>Paying customers</th><th>Credits</th></tr>"
                 f"{rows or '<tr><td colspan=5>No subscribers yet.</td></tr>'}"
                 "</table></div></div>")
        return _admin_page("Subscribers", inner, "admin-subscribers")

    @app.get("/admin/accounts", response_class=HTMLResponse)
    def admin_accounts(request: Request):
        _, redirect = _need_admin(request)
        if redirect is not None:
            return redirect
        rows = ""
        for account in _saas_store.list_accounts():
            sites = _saas_store.list_sites(account["id"])
            totp = _saas_store.totp_state(account["id"])
            reset_cell = ("—" if not totp["enabled"] else
                          f"<form method='post' action='/admin/accounts/"
                          f"{esc(account['id'])}/reset-totp'>"
                          f"<button type='submit'>Reset 2FA</button></form>")
            rows += (
                f"<tr><td>{esc(account['email'])}</td>"
                f"<td>{esc(account['name'] or '—')}</td>"
                f"<td>{esc(account['subscription_status'])}</td>"
                f"<td>{len(sites)}</td>"
                f"<td>{'Unlimited' if _saas_config.is_operator(account['email']) else account['credits_balance']}</td>"
                f"<td>{esc((account['created_at'] or '')[:10])}</td>"
                f"<td>{'Enabled' if totp['enabled'] else 'Not set up'}</td>"
                f"<td>{reset_cell}</td></tr>")
        inner = ("<div class='v-admin-card'>"
                 "<p class='muted'>Every account that exists on the "
                 "platform — subscribed or not. Counts only.</p>"
                 "<div class='table-wrap'><table>"
                 "<tr><th>Email</th><th>Name</th><th>Subscription</th>"
                 "<th>Sites</th><th>Credits</th><th>Created</th>"
                 "<th>Two-factor</th><th>Reset 2FA</th></tr>"
                 f"{rows or '<tr><td colspan=8>No accounts yet.</td></tr>'}"
                 "</table></div></div>")
        return _admin_page("Accounts", inner, "admin-accounts")

    @app.get("/admin/agents", response_class=HTMLResponse)
    def admin_agents(request: Request):
        _, redirect = _need_admin(request)
        if redirect is not None:
            return redirect
        orch = state["orchestrator"]
        aud: AuditLog = state["audit"]
        # Rows for every registered agent type, plus any type with
        # actual runs in the log even if it is not registered on
        # this runtime (a worker from another process, say). Types
        # that only ever appear in summary events (game, worker)
        # get no row — they never did agent work.
        types = list(orch.registry.agent_types())
        for e in aud.all_events():
            if e.event in _RUN_EVENTS and e.agent_type \
                    and e.agent_type not in types:
                types.append(e.agent_type)
        stats = {s["type"]: s for s in agent_stats_from_audit(aud, types)}
        problems = [e for e in aud.all_events()
                    if e.event in ("run_failed", "task_rework_requested")]
        failed_by_agent: dict[str, int] = {}
        for e in problems:
            failed_by_agent[e.agent_type] = \
                failed_by_agent.get(e.agent_type, 0) + 1
        stat_rows = "".join(
            f"<tr><td>{esc(t)}</td>"
            f"<td>{s['tasks_completed']}</td>"
            f"<td>{s['tokens_used']:,}</td>"
            f"<td>${s['cost_usd']:,.4f}</td>"
            f"<td>{failed_by_agent.get(t, 0)}</td>"
            f"<td>{esc(s['last_task']) or '—'}</td></tr>"
            for t, s in stats.items()
        ) or "<tr><td colspan='6'>No agent activity yet.</td></tr>"
        problem_rows = "".join(
            f"<tr><td>{esc(str(e.created_at)[:19])}</td>"
            f"<td>{esc(e.agent_type)}</td>"
            f"<td>{esc(e.event)}</td>"
            f"<td>{esc(e.business_id or '—')}</td>"
            f"<td>{esc(str(e.result or e.error or '')[:200])}</td></tr>"
            for e in problems[-20:]
        ) or ("<tr><td colspan='5'>No failed or reworked runs "
              "recorded.</td></tr>")
        inner = ("<div class='v-admin-card'>"
                 "<p class='muted'>What the agents are doing, which "
                 "need flagging or improvement, which did not work, and "
                 "what problems they are having. Aggregates and agent "
                 "health only — no customer portfolios, no approvals, "
                 "no approve buttons.</p>"
                 "<h2>Agent activity</h2>"
                 "<div class='table-wrap'><table>"
                 "<tr><th>Agent</th><th>Tasks</th><th>Tokens</th>"
                 "<th>Cost (USD)</th><th>Failed / reworked</th>"
                 "<th>Last task</th></tr>"
                 f"{stat_rows}</table></div></div>"
                 "<div class='v-admin-card'>"
                 "<h2>Problems (latest 20 failed or reworked runs)</h2>"
                 "<div class='table-wrap'><table>"
                 "<tr><th>When</th><th>Agent</th><th>Event</th>"
                 "<th>Business</th><th>Detail</th></tr>"
                 f"{problem_rows}</table></div></div>")
        return _admin_page("Agents", inner, "admin-agents")

    # -- Ventoryn SaaS shell (customer accounts, billing, credits) --------
    # The store/config were resolved at the top of create_app (so the
    # routes above close over the same store); plain create_app()
    # stays hermetic (tests, module import) and never creates a file.
    # The real dashboard passes a file-backed store via
    # create_dashboard_app().
    from ventoryn.saas_routes import register_saas_routes

    register_saas_routes(app, _saas_store, _saas_config)
    from ventoryn.analytics import register_analytics_routes
    register_analytics_routes(app, _saas_store, _saas_config)
    from ventoryn.public_files import register_public_files
    register_public_files(app, _saas_config)

    # Autopilot: the agents work on every website by default.
    def _start_autopilot():
        from ventoryn import autopilot
        from ventoryn.saas_routes import REPORTS_DIR as _reports

        app.state.autopilot_stop = autopilot.start(
            _saas_store, _saas_config, reports_dir=_reports,
            get_orchestrator=lambda: app.state.ecosystem["orchestrator"])

    def _stop_autopilot():
        stop = getattr(app.state, "autopilot_stop", None)
        if stop is not None:
            stop.set()

    app.router.on_startup.append(_start_autopilot)
    app.router.on_shutdown.append(_stop_autopilot)

    # -- activity: who is live now -----------------------------------------
    # Every authenticated request stamps the account's last-seen
    # (throttled to one write per minute per account in the store),
    # which is what the admin's "Live now" list reads. It runs after
    # the response so it can never slow or break a request — presence
    # is best-effort by design.
    @app.middleware("http")
    async def _touch_activity(request: Request, call_next):
        response = await call_next(request)
        try:
            account = _current_account(request)
            if account is not None:
                _saas_store.touch_activity(
                    account["id"], _activity_label(request.url.path))
        except Exception:  # noqa: BLE001 - presence never breaks a page
            pass
        return response

    # -- forced two-factor gate -------------------------------------
    # Two-factor is mandatory from the first sign-in. A session whose
    # account has not finished setup, or whose session has not passed
    # the code, is limited to the setup / verify pages (and logout, the
    # landing page, and static assets); every other signed-in route
    # redirects there. A session deleted server-side (or expired) is
    # simply no session — the route itself redirects unsigned visitors
    # to the landing page. The analyze preview needs no carve-out:
    # anonymous requests carry no session, so the gate passes them
    # through like any unsigned visitor, while a signed-in session
    # that has not finished 2FA is sent to setup like everywhere
    # else.
    _TOTP_FREE_PATHS = ("/", "/account/two-factor/setup",
                        "/account/two-factor/verify", "/account/logout",
                        "/account/auth/google/start",
                        "/account/auth/google/callback",
                        "/account/billing/webhook")

    @app.middleware("http")
    async def _totp_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith("/admin") or path.startswith("/static"):
            return await call_next(request)
        if path in _TOTP_FREE_PATHS:
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        session = _saas_store.get_session(token) if token else None
        if session is None:
            return await call_next(request)
        account = _saas_store.get_account(session["account_id"])
        if account is None:
            return await call_next(request)
        if session.get("totp_verified"):
            return await call_next(request)
        nxt = urllib.parse.quote(
            path + (("?" + request.url.query) if request.url.query else ""),
            safe="")
        if _saas_store.totp_state(account["id"])["enabled"]:
            dest = f"/account/two-factor/verify?next={nxt}"
        else:
            dest = f"/account/two-factor/setup?next={nxt}"
        return RedirectResponse(dest, status_code=303)

    # -- error pages in the platform shell ------------------------------
    # Unknown URLs used to return FastAPI's bare JSON
    # {"detail": "Not found"} — a signed-in user got no navigation
    # at all. A 404/500 now renders the platform shell (sidebar +
    # header) when signed in, and a bare centered card when not.
    # The shell render is best-effort: if it fails, the bare card
    # is the fallback, never a second crash.
    async def _shell_error_page(request: Request, status_code: int,
                                title: str, message: str):
        inner = (f"<div class='card'><h2>{esc(title)}</h2>"
                 f"<p>{esc(message)}</p>"
                 f"<p><a class='btn btn-primary' href='/ops'>"
                 f"Back to Overview</a></p></div>")
        try:
            account = _current_account(request)
        except Exception:  # noqa: BLE001 - error page never crashes
            account = None
        if account is None:
            return HTMLResponse(
                ui.page(title, inner), status_code=status_code)
        try:
            page = _operator_page(
                title, inner, account=account, current=None,
                is_operator=_saas_config.is_operator(account["email"]))
        except Exception:  # noqa: BLE001 - error page never crashes
            return HTMLResponse(
                ui.page(title, inner), status_code=status_code)
        return HTMLResponse(page, status_code=status_code)

    @app.exception_handler(404)
    async def _not_found_handler(request: Request, exc):
        return await _shell_error_page(
            request, 404, "Page not found",
            "That page does not exist — check the address, or go back "
            "to Overview.")

    @app.exception_handler(500)
    async def _server_error_handler(request: Request, exc):
        return await _shell_error_page(
            request, 500, "Something went wrong",
            "Ventoryn hit an unexpected error. Try again, or go back "
            "to Overview.")

    # Outermost layer, registered last: security headers on every
    # response and the cross-site request check (ventoryn/security.py).
    from ventoryn.security import install as _install_hardening
    _install_hardening(app, _saas_config)

    return app



def __getattr__(name: str):
    """`uvicorn ventoryn.app:app` still works, but importing this module
    no longer builds a whole runtime as a side effect."""
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)

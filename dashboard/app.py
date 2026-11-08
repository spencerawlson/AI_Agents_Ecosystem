"""Ventoryn dashboard: FastAPI app.

Public front door:
  GET /                    landing page (public, no sign-in) — what
                           Ventoryn is, the URL box + free preview, $20/mo

Customer pages (sign-in required; each account sees ONLY its own data):
  GET /ops, /game          Live Ops — the post-sign-in home for every
                           account, scoped to that account's sites
  GET /portfolio           the account's own sites/businesses
  GET /approvals           the account's own approvals (approve before
                           anything ships — nothing publishes unapproved)
  GET /report              the account's own growth report
  GET /account/...         the SaaS shell (see dashboard.saas_routes)

Operator tools (operator account only, by direct URL):
  GET /businesses, /businesses/{id}, /experiments, /experiment-001,
  GET /reports, /reports/{name}
  POST /api/tick, /api/reset

Admin (operator only, separate URL):
  GET /admin               -> /admin/subscribers
  GET /admin/subscribers   who subscribed
  GET /admin/accounts      all accounts on the platform
  GET /admin/agents        agent activity, health, problems (aggregates
                           only — never a customer's portfolio or
                           approvals, and no approve-on-behalf)

The operator account is the account whose email matches
SAAS_OPERATOR_EMAIL (see core.saas.SaasConfig). Spencer signs up
like any customer; his existing ecosystem businesses are attached
to that account by that email — the mechanism takes the email from
env config, it is never hardcoded.
"""

from __future__ import annotations

import html
import os
import re
import threading
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.audit import AuditLog
from core.experiments import ExperimentEngine
from core.ledger import Ledger
from core.registry import BusinessRegistry
from core.saas import SESSION_COOKIE
from orchestrator.approvals import ApprovalGate
from orchestrator.engine import Orchestrator


def esc(value: object) -> str:
    """HTML-escape any value interpolated into templates."""
    return html.escape(str(value), quote=True)


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
_PAGE_CSS = """
body { font-family: sans-serif; margin: 0 auto; max-width: 960px;
       padding: 16px; color: #222; }
.muted { color: #666; font-size: 14px; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 20px; }
th, td { border: 1px solid #ddd; padding: 8px; text-align: left;
         font-size: 14px; vertical-align: top; }
th { background: #f0f0f0; }
button { padding: 8px 14px; margin: 2px; border-radius: 6px;
         border: 1px solid #ccc; background: #fff; cursor: pointer;
         font-size: 14px; }
button[name="approved"][value="true"] { background: #e6f4ea;
         border-color: #34a853; }
button[name="approved"][value="false"] { background: #fce8e6;
         border-color: #ea4335; }
blockquote { border-left: 4px solid #f59e0b; margin: 12px 0;
             padding: 8px 12px; background: #fffbe6; }
code { background: #f0f0f0; padding: 1px 5px; border-radius: 4px;
       font-size: 13px; }
@media (max-width: 560px) { th, td { font-size: 13px; padding: 6px; } }
"""


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
) -> FastAPI:
    app = FastAPI(title="AI Business Ecosystem")
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
    from core.saas import SaasConfig, SaasStore

    _saas_store = saas_store or SaasStore(":memory:")
    _saas_config = saas_config or SaasConfig.from_env()
    app.state.saas_store = _saas_store
    app.state.saas_config = _saas_config

    def _current_account(request: Request):
        """The signed-in account from the session cookie, or None."""
        return _saas_store.get_account_by_session(
            request.cookies.get(SESSION_COOKIE, ""))

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

    def _owned_business_ids(account) -> set:
        """Registry businesses the account owns. The operator's
        businesses (Farelune, Road to CISSP, the Notion templates —
        the businesses already in the ecosystem state) are linked to
        the operator account by the SAAS_OPERATOR_EMAIL config; a
        regular customer owns no registry business, only the sites
        they added through the shell."""
        if _saas_config.is_operator(account["email"]):
            return {b.id for b in state["registry"].list()}
        return set()

    def _site_business_rows(account) -> list[dict]:
        """The account's own SaaS sites as business rows, so the
        portfolio, report and Live Ops render them like any other
        business. Revenue is real: the sum of recorded conversions
        for the site. There is no per-site dollar cost in the model,
        so costs read $0 — the page says so where it matters."""
        rows = []
        for site in _saas_store.list_sites(account["id"]):
            revenue = sum(
                c["amount_cents"]
                for c in _saas_store.list_conversions(site["id"])) / 100
            rows.append({
                "id": f"site:{site['id']}",
                "name": site["host"] or site["url"],
                "url": site["url"],
                "business_type": "website",
                "status": "active",
                "revenue": round(revenue, 2),
                "total_costs": 0.0,
                "net_profit": round(revenue, 2),
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
    # The customer nav (their own pages only — never an operator tool,
    # never another account's data): Live Ops, My Portfolio, Approvals,
    # Report, My account, Subscribe, Sign out. The admin nav is its
    # own thing (Subscribers, Accounts, Agents). Neither nav links
    # Experiments, Experiment 001, Daily plan, Attribution, or
    # "for website owners" — those stay reachable by direct URL only.
    _CUSTOMER_NAV = (
        "<nav class='customer-nav' aria-label='Ventoryn'>"
        "<a href='/ops'>Live Ops</a>"
        "<a href='/portfolio'>My Portfolio</a>"
        "<a href='/approvals'>Approvals</a>"
        "<a href='/report'>Report</a>"
        "<a href='/account/dashboard'>My account</a>"
        "<a href='/account/subscribe'>Subscribe</a>"
        "<a href='/account/logout'>Sign out</a></nav>")
    _ADMIN_NAV = (
        "<nav class='admin-nav' aria-label='Ventoryn admin'>"
        "<a href='/admin/subscribers'>Subscribers</a>"
        "<a href='/admin/accounts'>Accounts</a>"
        "<a href='/admin/agents'>Agents</a></nav>")
    _NAV_CSS = """
.customer-nav, .admin-nav { display: flex; flex-wrap: wrap; gap: 6px;
  margin: 0 0 16px; }
.customer-nav a, .admin-nav a { display: inline-block; min-height: 44px;
  line-height: 44px; padding: 0 14px; border-radius: 8px;
  background: #f3efff; border: 1px solid #c4b5fd; color: #111;
  text-decoration: none; font-size: 15px; }
.admin-nav a { background: #fff7ed; border-color: #fdba74; }
.customer-nav a:hover, .admin-nav a:hover { background: #e9e2ff; }
/* Portfolio: stat cards + styled business cards, like Live Ops —
   not a bare table. */
.stat-cards { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
.stat-card { flex: 1 1 140px; border: 1px solid #ddd; border-radius: 8px;
  padding: 12px; background: #fafafa; }
.stat-card .label { font-size: 12px; color: #666; text-transform: uppercase; }
.stat-card .value { font-size: 22px; font-weight: bold; margin-top: 4px; }
.biz-card { border: 1px solid #ddd; border-radius: 12px; padding: 16px;
  margin: 12px 0; background: #fff; }
.biz-name { font-size: 18px; font-weight: bold; }
.biz-sub { color: #666; font-size: 14px; margin: 4px 0 8px; }
.biz-nums { display: flex; gap: 16px; flex-wrap: wrap; font-size: 14px; }
.card { border: 1px solid #ddd; border-radius: 12px; padding: 16px;
  margin: 12px 0; background: #fff; }
"""

    def _operator_page(title: str, inner: str) -> str:
        """Shell for the signed-in pages: nav + content, mobile-first,
        the same light styling as the Live Ops page."""
        return (f"<html><head><title>{esc(title)} — Ventoryn</title>"
                f"<meta name='viewport' content='width=device-width,"
                f" initial-scale=1'><style>{_PAGE_CSS}{_NAV_CSS}</style>"
                f"</head><body>{_CUSTOMER_NAV}<h1>{esc(title)}</h1>"
                f"{inner}</body></html>")

    # -- public landing page ----------------------------------------------
    # GET / is the front door: what Ventoryn is, the URL box + free
    # preview, the $20/month price, and sign-in with Google — all in
    # one place. No sign-in needed to read it; no internal data on
    # it (no business names, revenue, costs, agent internals, no
    # Live Ops content); no links to /ops, /game or /portfolio for
    # a visitor who is not signed in. A signed-in visitor gets a
    # "Continue to Live Ops" button — /ops is their home.
    _LANDING_CSS = """
/* Ventoryn landing page: light, playful, colorful — the Live Ops
   visual language. Violet #C4B5FD is the action color, deep violet
   #6D28D9 the accent; soft lavender surfaces, white cards. */
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial,
  sans-serif; margin: 0; color: #1a1a2e; background: #f6f4ff; }
.wrap { max-width: 880px; margin: 0 auto; padding: 0 16px 40px; }
.hero { background: linear-gradient(135deg, #ede9fe 0%, #ddd6fe 55%,
  #c4b5fd 100%); text-align: center; padding: 48px 20px 40px; }
.hero-inner { max-width: 720px; margin: 0 auto; }
.tag { display: inline-block; background: #fff; color: #6d28d9;
  border-radius: 999px; padding: 6px 16px; font-weight: bold;
  font-size: 14px; box-shadow: 0 1px 4px rgba(0,0,0,0.12); }
.hero h1 { font-size: 42px; line-height: 1.12; margin: 16px 0 12px;
  color: #1a1a2e; }
.hero p { font-size: 19px; line-height: 1.5; color: #3f3f56;
  margin: 0 auto; max-width: 600px; }
.card { border: 1px solid #e5e0f5; border-radius: 16px; padding: 20px;
  margin: 20px 0; background: #fff;
  box-shadow: 0 2px 10px rgba(109, 40, 217, 0.06); }
.card h2 { margin-top: 0; font-size: 22px; color: #1a1a2e; }
.url-card { border: 2px solid #c4b5fd; background: #faf8ff; }
.url-card h2 { font-size: 24px; }
.muted { color: #666; font-size: 14px; }
.big { font-size: 36px; font-weight: bold; color: #6d28d9; }
.btn { display: inline-block; min-height: 44px; line-height: 44px;
  padding: 0 20px; margin: 4px 4px 4px 0; border-radius: 10px;
  border: 1px solid #ccc; background: #fff; color: #222; font-size: 16px;
  cursor: pointer; text-decoration: none; text-align: center;
  box-sizing: border-box; }
.btn-primary { background: #c4b5fd; border-color: #c4b5fd; color: #111;
  font-weight: bold; }
.btn-google { background: #fff; border-color: #888; }
label { display: block; margin: 12px 0 4px; font-weight: bold; }
input[type=text] { width: 100%; min-height: 52px;
  padding: 8px 14px; font-size: 18px; border: 2px solid #c4b5fd;
  border-radius: 10px; box-sizing: border-box; background: #fff; }
input[type=text]:focus { outline: none; border-color: #6d28d9; }
.steps { list-style: none; padding: 0; margin: 0; display: grid;
  grid-template-columns: 1fr 1fr; gap: 16px; }
.steps li { display: flex; gap: 12px; align-items: flex-start;
  background: #faf8ff; border: 1px solid #e5e0f5; border-radius: 12px;
  padding: 14px; }
.step-num { flex: 0 0 32px; height: 32px; border-radius: 50%;
  background: #c4b5fd; color: #111; font-weight: bold; text-align: center;
  line-height: 32px; }
.features { display: grid; grid-template-columns: repeat(3, 1fr);
  gap: 16px; }
.feature { border: 1px solid #e5e0f5; border-radius: 12px; padding: 16px;
  background: #fff; box-shadow: 0 2px 10px rgba(109, 40, 217, 0.06); }
.feature h3 { margin: 0 0 8px; font-size: 17px; color: #1a1a2e; }
.feature p { margin: 0; color: #444; font-size: 15px; line-height: 1.45; }
.price-card { background: linear-gradient(135deg, #ede9fe, #ddd6fe);
  border: 2px solid #c4b5fd; text-align: center; }
.price-card ul { list-style: none; padding: 0; margin: 16px auto;
  max-width: 420px; text-align: left; }
.price-card li { margin: 8px 0; font-size: 16px; }
.price-card li::before { content: "\2713  "; color: #6d28d9;
  font-weight: bold; }
.score-row { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
.score { flex: 1 1 140px; border: 1px solid #ddd; border-radius: 8px;
  padding: 12px; background: #fafafa; text-align: center; }
.score .label { font-size: 12px; color: #666; text-transform: uppercase; }
.score .value { font-size: 24px; font-weight: bold; margin-top: 4px; }
.findings { margin: 8px 0; padding-left: 20px; }
.findings li { margin: 6px 0; }
.footer { text-align: center; color: #888; font-size: 13px;
  margin: 24px 0 0; }
@media (max-width: 560px) {
  body { background: #fff; }
  .wrap { padding: 0 12px 32px; }
  .hero { padding: 32px 16px 28px; }
  .hero h1 { font-size: 28px; }
  .hero p { font-size: 17px; }
  .btn { width: 100%; margin: 4px 0; }
  .steps { grid-template-columns: 1fr; }
  .features { grid-template-columns: 1fr; }
  input[type=text] { font-size: 16px; }
}
"""

    def _landing_page(account) -> str:
        import json as _json

        signed_in = account is not None
        subscribed = bool(
            signed_in and account["subscription_status"] == "active")
        state_json = _json.dumps({"signedIn": signed_in,
                                  "subscribed": subscribed})
        if signed_in:
            signin_block = (
                "<div class='card'><p>You are signed in as "
                f"{esc(account['email'])}.</p>"
                "<p><a class='btn btn-primary' href='/ops'>"
                "Continue to Live Ops</a></p></div>")
        elif not _saas_config.google_configured:
            signin_block = (
                "<div class='card'><h2>Sign in</h2>"
                "<p>Sign-in with Google is not set up yet. The Google "
                "sign-in is configured by the operator (a Google Cloud "
                "OAuth client), not here — there is nothing to type in "
                "on this page.</p></div>")
        else:
            signin_block = (
                "<div class='card'><h2>Sign in</h2>"
                "<p>Ventoryn works on your website after you sign in — "
                "one Google account, no password to remember.</p>"
                "<p><a class='btn btn-google' id='landing-signin' "
                "href='/account/auth/google/start'>Sign in with Google"
                "</a></p>"
                "<p class='muted'>Ventoryn sees your Google name and "
                "email address, and nothing else.</p></div>")
        return f"""<html><head><title>Ventoryn — get your website its first 100 paying customers</title>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<style>{_LANDING_CSS}</style></head><body>
<div class='hero'>
<div class='hero-inner'>
<span class='tag'>Ventoryn</span>
<h1>Get your website its first 100 paying customers</h1>
<p>Type in your website URL. Ventoryn analyzes it, then a crew of AI
agents works on it every day — bringing it traffic and sales — until
it has 100 paying customers. Then 200.</p>
</div>
</div>
<div class='wrap'>
<div class='card url-card'>
<h2>🔍 Analyze your site — free preview</h2>
<p class='muted'>The same analysis Ventoryn runs before it starts work:
your site score, your answer-engine (GEO) score, and the top things
to fix. Nothing is published.</p>
<form id='landing-analyze-form'>
<label for='landing-url'>Your website URL</label>
<input id='landing-url' type='text' inputmode='url'
 placeholder='yourwebsite.com' autocomplete='off' required>
<p class='muted'>Just your domain works — type example.com, no https:// needed.</p>
<label for='landing-country'>Market country</label>
<input id='landing-country' type='text' value='ca' maxlength='2'
 autocomplete='off' required>
<p><button type='submit' class='btn btn-primary'
 id='landing-submit'>Analyze my site — free</button></p>
</form>
<div id='landing-status' class='muted' aria-live='polite'></div>
<div id='landing-preview' hidden></div>
<div id='landing-cta' hidden></div>
</div>
{signin_block}
<div class='features'>
<div class='feature'><h3>🛠️ A crew that works daily</h3>
<p>Eight AI agents — research, SEO, community, video and reports —
working on your site every day.</p></div>
<div class='feature'><h3>✅ You decide</h3>
<p>Nothing is published or posted without your approval. You pay
for it, you decide.</p></div>
<div class='feature'><h3>🎯 Real sales, not just traffic</h3>
<p>The goal is paying customers — your first 100 — not page views.</p></div>
</div>
<div class='card'>
<h2>How it works</h2>
<ol class='steps'>
<li><span class='step-num'>1</span><span><strong>Enter your URL.</strong>
 The free preview above shows what Ventoryn sees — and what it
 would fix first.</span></li>
<li><span class='step-num'>2</span><span><strong>Subscribe — $20/month.</strong>
 Sign in with Google, pay with Stripe. Ventoryn never sees your
 card.</span></li>
<li><span class='step-num'>3</span><span><strong>The agents work daily.</strong>
 SEO articles, community replies, site fixes, videos, reports —
 every day, until the site has paying customers.</span></li>
<li><span class='step-num'>4</span><span><strong>You approve before anything ships.</strong>
 Nothing is published or posted without your sign-off — you pay
 for it, you decide.</span></li>
</ol>
</div>
<div class='card price-card'>
<h2>$20/month</h2>
<p><span class='big'>$20</span> / month</p>
<ul>
<li>Your websites on Ventoryn</li>
<li>100 credits every month</li>
<li>A growth report per site</li>
<li>Agents working daily toward your first 100 paying customers</li>
</ul>
<p><a class='btn btn-primary' href='/account/subscribe'>Subscribe — $20/month</a></p>
</div>
<p class='footer'>Ventoryn — get your website its first 100 paying customers.</p>
</div>
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
</script>
</body></html>"""

    @app.get("/", response_class=HTMLResponse)
    def landing_page(request: Request):
        """Public front door — no sign-in, no internal data."""
        return _landing_page(_current_account(request))

    # -- My Portfolio: the account's own sites/businesses ---------------
    # Styled like Live Ops (stat cards + a styled list), not the bare
    # table it used to be. The operator sees his registry businesses
    # plus his own SaaS sites; a customer sees only their sites.
    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio(request: Request):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        led: Ledger = state["ledger"]
        if _saas_config.is_operator(account["email"]):
            biz_rows = []
            for b in state["registry"].list():
                pnl = led.pnl(b.id)
                biz_rows.append({
                    "id": b.id, "name": b.name,
                    "business_type": b.business_type,
                    "status": b.status.value,
                    "revenue": round(pnl.revenue, 2),
                    "total_costs": round(pnl.total_costs, 2),
                    "net_profit": round(pnl.net_profit, 2),
                })
            biz_rows += _site_business_rows(account)
        else:
            biz_rows = _site_business_rows(account)
        total_revenue = sum(r["revenue"] for r in biz_rows)
        total_profit = sum(r["net_profit"] for r in biz_rows)
        cards = "".join(
            f"<div class='biz-card'><div class='biz-name'>{esc(r['name'])}</div>"
            f"<div class='biz-sub'>{esc(r['business_type'])} · "
            f"{esc(r['status'])}</div>"
            f"<div class='biz-nums'><span>Revenue "
            f"${r['revenue']:,.2f}</span><span>Costs "
            f"${r['total_costs']:,.2f}</span><span>Net "
            f"${r['net_profit']:,.2f}</span></div></div>"
            for r in biz_rows) or (
            "<div class='card'><p>No websites yet. "
            "<a href='/account/sites/new'>Add your first website</a> — "
            "Ventoryn works to bring it its first 100 paying "
            "customers.</p></div>")
        inner = f"""<div class='stat-cards'>
<div class='stat-card'><div class='label'>Websites</div>
<div class='value'>{len(biz_rows)}</div></div>
<div class='stat-card'><div class='label'>Revenue</div>
<div class='value'>${total_revenue:,.2f}</div></div>
<div class='stat-card'><div class='label'>Net profit</div>
<div class='value'>${total_profit:,.2f}</div></div></div>
<p class='muted'>Costs per website read $0 here: the site's own costs
are its credits, and credits are not dollars. Revenue is real — the
sum of recorded paying customers per site.</p>
{cards}"""
        return _operator_page("My Portfolio", inner)

    @app.get("/businesses", response_class=HTMLResponse)
    def business_list(request: Request):
        _, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        reg: BusinessRegistry = state["registry"]
        led: Ledger = state["ledger"]
        businesses = reg.list()
        rows = "".join(
            f"<tr><td><a href='/businesses/{esc(b.id)}'>{esc(b.name)}</a></td>"
            f"<td>{esc(b.business_type)}</td><td>{esc(b.status.value)}</td>"
            f"<td>${led.pnl(b.id).net_profit:,.2f}</td></tr>"
            for b in businesses
        )
        return f"""<html><head><title>Businesses</title></head><body>
<h1>Businesses ({len(businesses)})</h1>
<table border="1"><tr><th>Name</th><th>Type</th><th>Status</th><th>Net profit</th></tr>{rows}</table>
<p><a href="/portfolio">My Portfolio</a></p>
</body></html>"""

    @app.get("/businesses/{business_id}", response_class=HTMLResponse)
    def business_detail(request: Request, business_id: str):
        _, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        reg: BusinessRegistry = state["registry"]
        led: Ledger = state["ledger"]
        aud: AuditLog = state["audit"]
        biz = reg.get(business_id)
        if biz is None:
            raise HTTPException(404, "business not found")
        pnl = led.pnl(business_id)
        events = aud.for_business(business_id)
        return f"""<html><head><title>{esc(biz.name)}</title></head><body>
<h1>{esc(biz.name)}</h1>
<p>Type: {esc(biz.business_type)} | Status: {esc(biz.status.value)}</p>
<h2>P&L</h2>
<p>Revenue: ${pnl.revenue:,.2f} | Costs: ${pnl.total_costs:,.2f} |
Net: ${pnl.net_profit:,.2f} | Net margin: {pnl.net_margin:.1%}</p>
<h2>Audit events ({len(events)})</h2>
<p><a href="/portfolio">My Portfolio</a></p>
</body></html>"""

    @app.get("/experiments", response_class=HTMLResponse)
    def experiment_list(request: Request):
        _, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        exp: ExperimentEngine = state["experiments"]
        rows = "".join(
            f"<tr><td>{esc(e.id)}</td><td>{esc(e.business_id)}</td>"
            f"<td>{esc(e.status.value)}</td><td>{esc(e.recommendation or '-')}</td></tr>"
            for e in exp._experiments.values()
        )
        return f"""<html><body><h1>Experiments</h1>
<table border="1"><tr><th>ID</th><th>Business</th><th>Status</th><th>Recommendation</th></tr>
{rows}</table><p><a href="/ops">Live Ops</a></p></body></html>"""

    @app.get("/approvals", response_class=HTMLResponse)
    def approval_queue(request: Request):
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
            if _saas_config.is_operator(account["email"]):
                return True
            return a.business_id in owned
        pending = [a for a in gate.pending() if _sees(a)]
        decided = [a for a in gate.decided(20) if _sees(a)]
        pend_rows = "".join(
            f"<tr><td>{_approval_summary(a, reg)}</td>"
            f"<td><form method='post' action='/approvals/{esc(a.id)}/decide'>"
            f"<button name='approved' value='true'>Approve</button>"
            f"<button name='approved' value='false'>Reject</button></form></td></tr>"
            for a in pending
        ) or "<tr><td colspan='2'>Nothing awaiting approval.</td></tr>"
        dec_rows = "".join(
            f"<tr><td>{_approval_summary(a, reg)}</td>"
            f"<td>{esc(a.status.value)}</td>"
            f"<td>{esc(a.decided_by or '—')}</td></tr>"
            for a in decided
        ) or "<tr><td colspan='3'>No decisions yet.</td></tr>"
        inner = f"""<p class="muted">Nothing is published or executed without your sign-off. Approving a mission report records your approval of its drafts &mdash; nothing is published automatically. Approving a <strong>supplier</strong> publishes that product to Shopify on the next publish run.</p>
<h2>Pending ({len(pending)})</h2>
<div class="table-wrap"><table>
<tr><th>Request</th><th>Decide</th></tr>{pend_rows}</table></div>
<h2>Recently decided</h2>
<div class="table-wrap"><table>
<tr><th>Request</th><th>Decision</th><th>By</th></tr>{dec_rows}</table></div>"""
        return _operator_page("✅ Approvals", inner)

    @app.post("/approvals/{approval_id}/decide")
    def decide_approval(request: Request, approval_id: str,
                        approved: str = Form(...)):
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
        if target is not None and not (
                _saas_config.is_operator(account["email"]) or
                target.business_id in _owned_business_ids(account)):
            return HTMLResponse(
                "<html><body><p>This approval is not yours to decide.</p>"
                "<p><a href='/approvals'>Back</a></p></body></html>",
                status_code=403)
        try:
            gate.decide(approval_id, approved=approved.lower() == "true",
                        decided_by=account["email"])
        except ValueError as exc:
            return HTMLResponse(
                f"<html><body><p>Could not record decision: {esc(exc)}</p>"
                f"<p><a href='/approvals'>Back</a></p></body></html>",
                status_code=400,
            )
        return HTMLResponse(
            "<html><body>Recorded. <a href='/approvals'>Back</a></body></html>"
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request):
        """Mission reports (e.g. marketing missions), newest first."""
        _, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        from dashboard.reports import list_reports

        items = list_reports()
        rows = "".join(
            f"<tr><td><a href='/reports/{esc(r['name'])}'>{esc(r['title'])}</a></td>"
            f"<td>{esc(r['mission'])}</td>"
            f"<td>{esc(r['date'] or r['generated_at'] or '—')}</td>"
            f"<td>{('$%.4f' % r['spend_usd']) if r['spend_usd'] is not None else '—'}</td></tr>"
            for r in items
        ) or ("<tr><td colspan='4'>No mission reports yet. Run a mission "
                "(e.g. <code>python3 ecosystem/marketing_mission.py --llm</code>) "
                "to produce one.</td></tr>")
        return f"""<html><head><title>Mission reports</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_PAGE_CSS}</style></head><body>
<h1>📄 Mission reports</h1>
<p class="muted">Agent mission outputs, newest first. Reports are drafts —
nothing in them was published.</p>
<div class="table-wrap"><table>
<tr><th>Report</th><th>Mission</th><th>Date</th><th>AI spend</th></tr>
{rows}</table></div>
<p><a href="/approvals">✅ Approvals</a></p>
</body></html>"""

    @app.get("/reports/{name}", response_class=HTMLResponse)
    def report_view(request: Request, name: str):
        """Render one mission report as safe HTML."""
        _, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        from dashboard.reports import render_report

        result = render_report(name)
        if result is None:
            raise HTTPException(404, "report not found")
        title, body = result
        return f"""<html><head><title>{esc(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_PAGE_CSS}</style></head><body>
{body}
<p><a href="/reports">Back to reports</a></p>
</body></html>"""

    @app.get("/experiment-001", response_class=HTMLResponse)
    def experiment_001(request: Request):
        """Live Experiment 001 (Evergreen Planners / Etsy) status."""
        _, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        try:
            from ecosystem.etsy_monitor import (
                EtsyCredentialsError,
                check_experiment_001,
            )

            snapshot = check_experiment_001()
        except EtsyCredentialsError as exc:
            return (
                f"<html><body><h1>Experiment 001</h1>"
                f"<p>Etsy not configured: {esc(exc)}</p>"
                f"<p><a href='/ops'>Live Ops</a></p></body></html>"
            )
        except Exception as exc:  # noqa: BLE001 - dashboard must stay up
            return (
                f"<html><body><h1>Experiment 001</h1>"
                f"<p>Monitor error: {esc(exc)}</p>"
                f"<p><a href='/ops'>Live Ops</a></p></body></html>"
            )
        triggers = "".join(
            f"<li>{esc(t)}</li>" for t in snapshot["stop_loss_triggers"]
        ) or "<li>none</li>"
        color = {"ON_TRACK": "green", "BEHIND": "orange",
                 "STOP_LOSS": "red"}[snapshot["verdict"]]
        return f"""<html><body><h1>Experiment 001 — Evergreen Planners</h1>
<p>Checked: {esc(snapshot["checked_at"])}</p>
<p>Day {snapshot["days_elapsed"]}/60 ({snapshot["days_remaining"]} remaining)</p>
<h2 style="color:{color}">{snapshot["verdict"]}</h2>
<table border="1">
<tr><th>Metric</th><th>Actual</th><th>Target</th></tr>
<tr><td>Revenue</td><td>${snapshot["revenue_usd"]:,.2f}</td>
    <td>${snapshot["target_revenue_usd"]:,.2f}</td></tr>
<tr><td>Orders</td><td>{snapshot["orders"]}</td>
    <td>{snapshot["target_orders"]}</td></tr>
<tr><td>Active listings</td><td>{snapshot["active_listings"]}</td><td>12</td></tr>
<tr><td>Spend</td><td>${snapshot["spend_usd"]:,.2f}</td><td>$91.00</td></tr>
<tr><td>Contribution margin</td><td>${snapshot["contribution_margin_usd"]:,.2f}</td><td>&gt; $0</td></tr>
<tr><td>Pace vs target</td><td>{snapshot["pace_pct"]}%</td><td>100%</td></tr>
</table>
<h2>Stop-loss triggers</h2>
<ul>{triggers}</ul>
<h2>Weekly revenue (last 3 weeks)</h2>
<p>{", ".join(f"${w:,.2f}" for w in snapshot["weekly_revenue_usd"])}</p>
<p><a href="/ops">Live Ops</a></p></body></html>"""

    # -- Live Ops API (powers the /ops live view; /game is the old address) ---
    # All state transitions go through the real runtime components; these
    # endpoints only serialize what the orchestrator/ledger/audit produced.
    from fastapi.responses import JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)),
                  name="static")

    @app.get("/game", response_class=HTMLResponse)
    @app.get("/ops", response_class=HTMLResponse)
    def game_view(request: Request):
        """Ventoryn live-ops view of the local runtime. Sign-in
        required — /ops is the post-sign-in home for every account,
        and the page is scoped to the signed-in account by the
        snapshot it polls. Not signed in -> the landing page."""
        _, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        page = static_dir / "game.html"
        if page.exists():
            return FileResponse(str(page), media_type="text/html")
        return HTMLResponse(
            "<html><body><h1>Live Ops view not built yet</h1>"
            "<p><a href='/'>Back</a></p></body></html>", status_code=503)

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
        aud: AuditLog = state["audit"]
        operator = account is None or _saas_config.is_operator(
            account["email"])
        if operator:
            businesses = reg.list()
            biz_rows = []
            for b in businesses:
                pnl = led.pnl(b.id)
                biz_rows.append({
                    "id": b.id, "name": b.name,
                    "business_type": b.business_type,
                    "status": b.status.value,
                    "revenue": round(pnl.revenue, 2),
                    "total_costs": round(pnl.total_costs, 2),
                    "net_profit": round(pnl.net_profit, 2),
                })
            if account is not None:
                # The operator's own SaaS sites ride along with his
                # businesses — his account sees everything of his.
                biz_rows += _site_business_rows(account)
            events = [{
                "ts": e.created_at.isoformat()
                if hasattr(e.created_at, "isoformat")
                else str(e.created_at),
                "actor": e.agent_type, "event": e.event,
                "business_id": e.business_id, "task_id": e.task_id,
                "result": e.result,
                "cost_usd": round(e.cost_usd, 4),
                "tokens_used": e.tokens_used,
            } for e in aud.latest(50)]
            agents = _agent_snapshot()
            exp = game["experiment_001"]
        else:
            biz_rows = _site_business_rows(account)
            sites_by_id = {s["id"]: s
                           for s in _saas_store.list_sites(account["id"])}
            events = []
            for job in _saas_store.list_jobs(account["id"], limit=50):
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
            agents = [{
                "type": t, "tasks_completed": 0, "tokens_used": 0,
                "cost_usd": 0.0, "last_task": "",
            } for t in state["orchestrator"].registry.agent_types()]
            exp = None
        totals = {
            "revenue": round(sum(r["revenue"] for r in biz_rows), 2),
            "total_costs": round(sum(r["total_costs"] for r in biz_rows), 2),
            "net_profit": round(sum(r["net_profit"] for r in biz_rows), 2),
            "businesses": len(biz_rows),
        }
        return {
            "tick": game["tick"],
            "businesses": biz_rows,
            "agents": agents,
            "totals": totals,
            "events": events,
            "experiment_001": exp,
            "account": _account_json(account) if account else None,
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

    @app.get("/api/daily-plan")
    def api_daily_plan():
        """Today's ordered marketing moves for Road to CISSP, as JSON.

        Pure read + build from the growth state file — no agent is
        dispatched, no money is spent, nothing is published. The state
        file is the default growth state unless DAILY_PLAN_STATE_PATH
        points elsewhere (tests use that to point at a tmp file).
        Building the plan here does not persist it; the CLI
        (``daily-plan``) records it so tomorrow can carry items over.
        """
        from ecosystem import daily_plan
        from ecosystem.road2cissp_growth import (
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
    def api_attribution():
        """Per-channel funnel attribution for Road to CISSP, as JSON.

        Pure read from the growth state file (same file the daily
        plan reads, ``DAILY_PLAN_STATE_PATH`` when set): recorded
        conversions attributed per channel, sessions from the latest
        recorded Plausible snapshot. No Plausible fetch here, no agent
        dispatched, no money spent. The CLI (``attribution``) is the
        one that fetches fresh Plausible data and writes the report.
        """
        from core.attribution import build_attribution
        from ecosystem.road2cissp_growth import (
            default_state_path, load_state,
        )

        state_path = os.environ.get("DAILY_PLAN_STATE_PATH")
        state = load_state(Path(state_path) if state_path
                           else default_state_path())
        return JSONResponse(build_attribution(state))

    # -- Site analysis jobs (powers the /ops "Analyze a site" panel) ---
    # The same pipeline as the `analyze` CLI, run in a background
    # thread: POST starts the job, the page polls GET for the result.
    # Nothing is published or posted — the only output is the local
    # report file (the same file the CLI writes) plus the on-page
    # summary. `app.state.analyze_runner` is None in production;
    # tests inject a fast fake runner.
    from dashboard.analyze_jobs import get_job as _analyze_get_job
    from dashboard.analyze_jobs import new_job_store as _analyze_new_store
    from dashboard.analyze_jobs import start_job as _analyze_start_job
    from dashboard.analyze_jobs import validate_country as _validate_country
    from dashboard.analyze_jobs import validate_language as _validate_language
    from dashboard.analyze_jobs import validate_url as _validate_analyze_url

    app.state.analyze_jobs = _analyze_new_store()
    app.state.analyze_runner = None

    @app.post("/api/analyze")
    def api_analyze_start(payload: dict):
        payload = payload or {}
        try:
            url = _validate_analyze_url(payload.get("url", ""))
            country = _validate_country(payload.get("country", ""))
            language = _validate_language(payload.get("language", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        job = _analyze_start_job(
            app.state.analyze_jobs, url, country, language,
            runner=app.state.analyze_runner)
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
        from dashboard.reports import reports_dir

        job = _analyze_get_job(app.state.analyze_jobs, job_id)
        if job is None or not job.get("report_name"):
            raise HTTPException(404, "report not ready")
        base = reports_dir()
        path = (base / job["report_name"]).resolve()
        if path.parent != base.resolve() or not path.is_file():
            raise HTTPException(404, "report not ready")
        return FileResponse(str(path), media_type="text/markdown",
                            filename=job["report_name"])

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
        if not _saas_config.is_operator(account["email"]):
            site_rows = "".join(
                f"<tr><td>{esc(s['url'])}</td>"
                f"<td>{_saas_store.paying_customer_count(s['id'])}</td>"
                f"<td>${sum(c['amount_cents'] for c in _saas_store.list_conversions(s['id'])) / 100:,.2f}</td></tr>"
                for s in _saas_store.list_sites(account["id"])
            ) or "<tr><td colspan='3'>No websites yet.</td></tr>"
            job_rows = "".join(
                f"<tr><td>{esc(j['job_type'].replace('_', ' '))}</td>"
                f"<td>{esc(j['status'])}</td>"
                f"<td>{j['credits_charged']} credits</td>"
                f"<td>{esc(j['result'] or '—')}</td></tr>"
                for j in _saas_store.list_jobs(account["id"])
            ) or "<tr><td colspan='4'>No jobs yet.</td></tr>"
            inner = f"""<p>Generated: {esc(utcnow().isoformat())}</p>
<div class='stat-cards'>
<div class='stat-card'><div class='label'>Revenue</div>
<div class='value'>${fin["revenue"]:,.2f}</div></div>
<div class='stat-card'><div class='label'>Paying customers</div>
<div class='value'>{sum(_saas_store.paying_customer_count(s['id']) for s in _saas_store.list_sites(account['id']))}</div></div>
<div class='stat-card'><div class='label'>Credit balance</div>
<div class='value'>{account['credits_balance']}</div></div></div>
<p class='muted'>Revenue is the sum of recorded paying customers
across your sites. Visitors read "unavailable" until Plausible is
connected on a site — see its page under My account.</p>
<h2>Your websites</h2>
<div class="table-wrap"><table>
<tr><th>Website</th><th>Paying customers</th><th>Revenue</th></tr>
{site_rows}</table></div>
<h2>Recent jobs</h2>
<div class="table-wrap"><table>
<tr><th>Job</th><th>Status</th><th>Cost</th><th>Result</th></tr>
{job_rows}</table></div>"""
            return _operator_page("📊 Report", inner)
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
            f"<td>${b['revenue']:,.2f}</td>"
            f"<td>${b['total_costs']:,.2f}</td>"
            f"<td>${b['net_profit']:,.2f}</td></tr>"
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
        net_color = "green" if fin["net_profit"] >= 0 else "red"
        return f"""<html><head><title>Agent &amp; Financial Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: sans-serif; margin: 0 auto; max-width: 960px;
       padding: 16px; color: #222; }}
.cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }}
.card {{ flex: 1 1 140px; border: 1px solid #ddd; border-radius: 8px;
        padding: 12px; background: #fafafa; }}
.card .label {{ font-size: 12px; color: #666; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; margin-top: 4px; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0 20px; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left;
         font-size: 14px; }}
th {{ background: #f0f0f0; }}
@media (max-width: 560px) {{ th, td {{ font-size: 13px; padding: 6px; }} }}
</style><style>{_NAV_CSS}</style></head><body>{_CUSTOMER_NAV}
<h1>📊 Agent &amp; Financial Report</h1>
<p>Generated: {esc(utcnow().isoformat())} · Tick: {snap["tick"]}</p>
<h2>Financials</h2>
<div class="cards">
<div class="card"><div class="label">Revenue</div>
<div class="value">${fin["revenue"]:,.2f}</div></div>
<div class="card"><div class="label">Total costs</div>
<div class="value">${fin["total_costs"]:,.2f}</div></div>
<div class="card"><div class="label">Net profit</div>
<div class="value" style="color:{net_color}">${fin["net_profit"]:,.2f}</div></div>
</div>
<h2>Agent performance</h2>
<div class="table-wrap"><table>
<tr><th>Agent</th><th>Tasks</th><th>Tokens</th><th>Cost (USD)</th>
<th>Last task</th></tr>{agent_rows}</table></div>
<h2>Business P&amp;L</h2>
<div class="table-wrap"><table>
<tr><th>Name</th><th>Type</th><th>Status</th><th>Revenue</th><th>Costs</th>
<th>Net profit</th></tr>{biz_rows}</table></div>
{exp_block}
</body></html>"""

    @app.post("/api/tick")
    def api_tick(request: Request):
        """One orchestrator tick. Operator only — a customer must
        never be able to drive the simulation (or reset it)."""
        _, redirect = _need_operator(request)
        if redirect is not None:
            return JSONResponse({"ok": False, "error": "operator only"},
                                status_code=403)
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
    if not getattr(app.state, "runtime_holder", None):
        from ecosystem.runtime import build_runtime as _br

        app.state.runtime_holder = {"rt": _br(
            use_postgres=bool(os.environ.get("DATABASE_URL")))}

    def _rt():
        return app.state.runtime_holder["rt"]

    # -- /admin: operator oversight, separate URL --------------------------
    # ventoryn.app/admin is the operator home — and ONLY this:
    # who subscribed, all accounts, and what the agents are doing
    # (activity, health, problems). Aggregates only: the admin does
    # NOT get any customer's portfolio or approvals — those are the
    # customer's own financial decisions, made alone. No approve
    # buttons for customer items anywhere here, and no per-customer
    # site URLs or funnels.
    def _admin_page(title: str, inner: str) -> str:
        return (f"<html><head><title>{esc(title)} — Ventoryn admin</title>"
                f"<meta name='viewport' content='width=device-width,"
                f" initial-scale=1'><style>{_PAGE_CSS}{_NAV_CSS}</style>"
                f"</head><body>{_ADMIN_NAV}<h1>{esc(title)}</h1>"
                f"{inner}</body></html>")

    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request):
        _, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        return RedirectResponse("/admin/subscribers", status_code=303)

    @app.get("/admin/subscribers", response_class=HTMLResponse)
    def admin_subscribers(request: Request):
        _, redirect = _need_operator(request)
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
                f"<td>{account['credits_balance']}</td></tr>")
        inner = ("<p class='muted'>Every account with an active "
                 "$20/month subscription. Counts only — no site URLs, "
                 "no funnels, no approvals: those belong to the "
                 "customer.</p>"
                 "<div class='table-wrap'><table>"
                 "<tr><th>Email</th><th>Name</th><th>Sites</th>"
                 "<th>Paying customers</th><th>Credits</th></tr>"
                 f"{rows or '<tr><td colspan=5>No subscribers yet.</td></tr>'}"
                 "</table></div>")
        return _admin_page("Subscribers", inner)

    @app.get("/admin/accounts", response_class=HTMLResponse)
    def admin_accounts(request: Request):
        _, redirect = _need_operator(request)
        if redirect is not None:
            return redirect
        rows = ""
        for account in _saas_store.list_accounts():
            sites = _saas_store.list_sites(account["id"])
            rows += (
                f"<tr><td>{esc(account['email'])}</td>"
                f"<td>{esc(account['name'] or '—')}</td>"
                f"<td>{esc(account['subscription_status'])}</td>"
                f"<td>{len(sites)}</td>"
                f"<td>{account['credits_balance']}</td>"
                f"<td>{esc((account['created_at'] or '')[:10])}</td></tr>")
        inner = ("<p class='muted'>Every account that exists on the "
                 "platform — subscribed or not. Counts only.</p>"
                 "<div class='table-wrap'><table>"
                 "<tr><th>Email</th><th>Name</th><th>Subscription</th>"
                 "<th>Sites</th><th>Credits</th><th>Created</th></tr>"
                 f"{rows or '<tr><td colspan=6>No accounts yet.</td></tr>'}"
                 "</table></div>")
        return _admin_page("Accounts", inner)

    @app.get("/admin/agents", response_class=HTMLResponse)
    def admin_agents(request: Request):
        _, redirect = _need_operator(request)
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
        inner = ("<p class='muted'>What the agents are doing, which "
                 "need flagging or improvement, which did not work, and "
                 "what problems they are having. Aggregates and agent "
                 "health only — no customer portfolios, no approvals, "
                 "no approve buttons.</p>"
                 "<h2>Agent activity</h2>"
                 "<div class='table-wrap'><table>"
                 "<tr><th>Agent</th><th>Tasks</th><th>Tokens</th>"
                 "<th>Cost (USD)</th><th>Failed / reworked</th>"
                 "<th>Last task</th></tr>"
                 f"{stat_rows}</table></div>"
                 "<h2>Problems (latest 20 failed or reworked runs)</h2>"
                 "<div class='table-wrap'><table>"
                 "<tr><th>When</th><th>Agent</th><th>Event</th>"
                 "<th>Business</th><th>Detail</th></tr>"
                 f"{problem_rows}</table></div>")
        return _admin_page("Agents", inner)

    # -- Ventoryn SaaS shell (customer accounts, billing, credits) --------
    # The store/config were resolved at the top of create_app (so the
    # routes above close over the same store); plain create_app()
    # stays hermetic (tests, module import) and never creates a file.
    # The real dashboard passes a file-backed store via
    # create_dashboard_app().
    from dashboard.saas_routes import register_saas_routes

    register_saas_routes(app, _saas_store, _saas_config)

    return app


app = create_app()

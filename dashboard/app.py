"""Portfolio dashboard: FastAPI app.

Routes:
  GET /                    portfolio overview
  GET /businesses          business list
  GET /businesses/{id}     business drill-down (P&L, experiments, audit)
  GET /experiments         experiment list
  GET /approvals           approval queue
  POST /approvals/{id}/decide  approve/reject
"""

from __future__ import annotations

import html
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse

from core.audit import AuditLog
from core.experiments import ExperimentEngine
from core.ledger import Ledger
from core.registry import BusinessRegistry
from orchestrator.approvals import ApprovalGate
from orchestrator.engine import Orchestrator


def esc(value: object) -> str:
    """HTML-escape any value interpolated into templates."""
    return html.escape(str(value), quote=True)


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
) -> FastAPI:
    app = FastAPI(title="AI Business Ecosystem")
    state = {
        "orchestrator": orchestrator or Orchestrator(),
        "registry": registry or BusinessRegistry(),
        "ledger": ledger or Ledger(),
        "experiments": experiments or ExperimentEngine(),
        "approvals": approvals or ApprovalGate(),
        "audit": audit or AuditLog(),
    }
    app.state.ecosystem = state

    @app.get("/", response_class=HTMLResponse)
    def portfolio():
        reg: BusinessRegistry = state["registry"]
        led: Ledger = state["ledger"]
        businesses = reg.list()
        total_revenue = sum(led.pnl(b.id).revenue for b in businesses)
        total_profit = sum(led.pnl(b.id).net_profit for b in businesses)
        rows = "".join(
            f"<tr><td><a href='/businesses/{esc(b.id)}'>{esc(b.name)}</a></td>"
            f"<td>{esc(b.business_type)}</td><td>{esc(b.status.value)}</td>"
            f"<td>${led.pnl(b.id).net_profit:,.2f}</td></tr>"
            for b in businesses
        )
        return f"""<html><head><title>Portfolio</title></head><body>
<h1>Portfolio</h1>
<p>Businesses: {len(businesses)} | Revenue: ${total_revenue:,.2f} | Net profit: ${total_profit:,.2f}</p>
<table border="1"><tr><th>Name</th><th>Type</th><th>Status</th><th>Net profit</th></tr>{rows}</table>
<p><a href="/experiments">Experiments</a> | <a href="/approvals">Approvals</a> | <a href="/reports">📄 Reports</a> | <a href="/experiment-001">Experiment 001</a> | <a href="/report">📊 Report</a> | <a href="/game">🎮 Live game view</a></p>
</body></html>"""

    @app.get("/businesses", response_class=HTMLResponse)
    def business_list():
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
<p><a href="/">Back</a></p>
</body></html>"""

    @app.get("/businesses/{business_id}", response_class=HTMLResponse)
    def business_detail(business_id: str):
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
<p><a href="/">Back</a></p>
</body></html>"""

    @app.get("/experiments", response_class=HTMLResponse)
    def experiment_list():
        exp: ExperimentEngine = state["experiments"]
        rows = "".join(
            f"<tr><td>{esc(e.id)}</td><td>{esc(e.business_id)}</td>"
            f"<td>{esc(e.status.value)}</td><td>{esc(e.recommendation or '-')}</td></tr>"
            for e in exp._experiments.values()
        )
        return f"""<html><body><h1>Experiments</h1>
<table border="1"><tr><th>ID</th><th>Business</th><th>Status</th><th>Recommendation</th></tr>
{rows}</table><p><a href="/">Back</a></p></body></html>"""

    @app.get("/approvals", response_class=HTMLResponse)
    def approval_queue():
        gate: ApprovalGate = state["approvals"]
        reg: BusinessRegistry = state["registry"]
        pending = gate.pending()
        decided = gate.decided(20)
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
        return f"""<html><head><title>Approvals</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_PAGE_CSS}</style></head><body>
<h1>✅ Approvals</h1>
<p class="muted">Nothing is published or executed without your sign-off. Approving a mission report records your approval of its drafts &mdash; nothing is published automatically. Approving a <strong>supplier</strong> publishes that product to Shopify on the next publish run.</p>
<h2>Pending ({len(pending)})</h2>
<div class="table-wrap"><table>
<tr><th>Request</th><th>Decide</th></tr>{pend_rows}</table></div>
<h2>Recently decided</h2>
<div class="table-wrap"><table>
<tr><th>Request</th><th>Decision</th><th>By</th></tr>{dec_rows}</table></div>
<p><a href="/">Back to portfolio</a> | <a href="/reports">📄 Reports</a></p>
</body></html>"""

    @app.post("/approvals/{approval_id}/decide")
    def decide_approval(approval_id: str, approved: str = Form(...)):
        gate: ApprovalGate = state["approvals"]
        try:
            gate.decide(approval_id, approved=approved.lower() == "true",
                        decided_by="owner")
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
    def reports_page():
        """Mission reports (e.g. marketing missions), newest first."""
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
<p><a href="/">Back to portfolio</a> | <a href="/approvals">✅ Approvals</a></p>
</body></html>"""

    @app.get("/reports/{name}", response_class=HTMLResponse)
    def report_view(name: str):
        """Render one mission report as safe HTML."""
        from dashboard.reports import render_report

        result = render_report(name)
        if result is None:
            raise HTTPException(404, "report not found")
        title, body = result
        return f"""<html><head><title>{esc(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_PAGE_CSS}</style></head><body>
{body}
<p><a href="/reports">Back to reports</a> | <a href="/">Portfolio</a></p>
</body></html>"""

    @app.get("/experiment-001", response_class=HTMLResponse)
    def experiment_001():
        """Live Experiment 001 (Evergreen Planners / Etsy) status."""
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
                f"<p><a href='/'>Back</a></p></body></html>"
            )
        except Exception as exc:  # noqa: BLE001 - dashboard must stay up
            return (
                f"<html><body><h1>Experiment 001</h1>"
                f"<p>Monitor error: {esc(exc)}</p>"
                f"<p><a href='/'>Back</a></p></body></html>"
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
<p><a href="/">Back</a></p></body></html>"""

    # -- Game API (powers the /game live view) -------------------------------
    # All state transitions go through the real runtime components; these
    # endpoints only serialize what the orchestrator/ledger/audit produced.
    from fastapi.responses import JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)),
                  name="static")

    @app.get("/game", response_class=HTMLResponse)
    def game_view():
        """Game-style live view of the local runtime."""
        page = static_dir / "game.html"
        if page.exists():
            return FileResponse(str(page), media_type="text/html")
        return HTMLResponse(
            "<html><body><h1>Game view not built yet</h1>"
            "<p><a href='/'>Back</a></p></body></html>", status_code=503)

    game = {
        "tick": 0,
        "agent_stats": {},  # agent_type -> {tasks, tokens, cost_usd, last_task}
        "experiment_001": None,
    }
    app.state.game = game

    def _agent_snapshot() -> list[dict]:
        orch = state["orchestrator"]
        stats = game["agent_stats"]
        out = []
        for agent_type in orch.registry.agent_types():
            s = stats.get(agent_type, {})
            out.append({
                "type": agent_type,
                "tasks_completed": s.get("tasks", 0),
                "tokens_used": s.get("tokens", 0),
                "cost_usd": round(s.get("cost", 0.0), 4),
                "last_task": s.get("last_task", ""),
            })
        return out

    def _snapshot() -> dict:
        reg: BusinessRegistry = state["registry"]
        led: Ledger = state["ledger"]
        aud: AuditLog = state["audit"]
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
        events = [{
            "ts": e.created_at.isoformat() if hasattr(e.created_at, "isoformat")
            else str(e.created_at),
            "actor": e.agent_type, "event": e.event,
            "business_id": e.business_id, "task_id": e.task_id,
            "result": e.result,
        } for e in aud.latest(50)]
        totals = {
            "revenue": round(sum(r["revenue"] for r in biz_rows), 2),
            "total_costs": round(sum(r["total_costs"] for r in biz_rows), 2),
            "net_profit": round(sum(r["net_profit"] for r in biz_rows), 2),
            "businesses": len(biz_rows),
        }
        return {
            "tick": game["tick"],
            "businesses": biz_rows,
            "agents": _agent_snapshot(),
            "totals": totals,
            "events": events,
            "experiment_001": game["experiment_001"],
        }

    @app.get("/api/snapshot")
    def api_snapshot():
        return JSONResponse(_snapshot())

    @app.get("/api/report")
    def api_report():
        """Agent + financial report as JSON. Composed from _snapshot() so the
        composition logic lives in one place; blended margin comes from
        core.reporting.FinancialReporting."""
        from core.reporting import FinancialReporting
        from orchestrator.models import utcnow

        snap = _snapshot()
        exp = snap["experiment_001"]
        summary = FinancialReporting(state["ledger"]).portfolio_summary(
            [b["id"] for b in snap["businesses"]])
        return JSONResponse({
            "generated_at": utcnow().isoformat(),
            "tick": snap["tick"],
            "agents": snap["agents"],
            "financials": {
                "revenue": snap["totals"]["revenue"],
                "total_costs": snap["totals"]["total_costs"],
                "net_profit": snap["totals"]["net_profit"],
                "blended_margin": round(summary["blended_margin"], 4),
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

    @app.get("/report", response_class=HTMLResponse)
    def report_page():
        """Server-rendered agent + financial report (mobile-friendly)."""
        from orchestrator.models import utcnow

        snap = _snapshot()
        exp = snap["experiment_001"]
        fin = {
            "revenue": snap["totals"]["revenue"],
            "total_costs": snap["totals"]["total_costs"],
            "net_profit": snap["totals"]["net_profit"],
        }
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
</style></head><body>
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
<p><a href="/">Back to portfolio</a> | <a href="/reports">📄 Reports</a> | <a href="/game">🎮 Live game view</a></p>
</body></html>"""

    @app.post("/api/tick")
    def api_tick():
        from ecosystem.worker import run_etsy_monitor_tick, run_tick

        game["tick"] += 1
        tick = game["tick"]
        try:
            result = run_tick(_rt(), tick)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc),
                                 "snapshot": _snapshot()})
        if result.get("ok"):
            for run in result.get("runs", []):
                s = game["agent_stats"].setdefault(run["agent_type"], {})
                s["tasks"] = s.get("tasks", 0) + 1
                s["tokens"] = s.get("tokens", 0) + run["tokens_used"]
                s["cost"] = s.get("cost", 0.0) + run["cost_usd"]
                s["last_task"] = f"tick {tick}: {result.get('winner')}"
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
    def api_reset():
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
        game["agent_stats"] = {}
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

    return app


app = create_app()

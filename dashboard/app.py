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
<p><a href="/experiments">Experiments</a> | <a href="/approvals">Approvals</a></p>
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
        rows = "".join(
            f"<tr><td>{esc(a.id)}</td><td>{esc(a.action)}</td><td>{a.amount_usd}</td>"
            f"<td><form method='post' action='/approvals/{esc(a.id)}/decide'>"
            f"<button name='approved' value='true'>Approve</button>"
            f"<button name='approved' value='false'>Reject</button></form></td></tr>"
            for a in gate.pending()
        )
        return f"""<html><body><h1>Approval queue</h1>
<table border="1"><tr><th>ID</th><th>Action</th><th>Amount</th><th>Decide</th></tr>
{rows}</table><p><a href="/">Back</a></p></body></html>"""

    @app.post("/approvals/{approval_id}/decide")
    def decide_approval(approval_id: str, approved: str = Form(...)):
        gate: ApprovalGate = state["approvals"]
        gate.decide(approval_id, approved=approved.lower() == "true", decided_by="owner")
        return HTMLResponse(
            "<html><body>Recorded. <a href='/approvals'>Back</a></body></html>"
        )

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

    return app


app = create_app()

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

    return app


app = create_app()

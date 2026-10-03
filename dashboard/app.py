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
<p><a href="/experiments">Experiments</a> | <a href="/approvals">Approvals</a> | <a href="/experiment-001">Experiment 001</a> | <a href="/game">🎮 Live game view</a></p>
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
        rt = build_runtime(use_postgres=use_pg)
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

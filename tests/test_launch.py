"""Tests for the launch wiring: runtime composition root and worker tick."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ecosystem.runtime import AGENT_CLASSES, build_runtime
from ecosystem.worker import run_tick


def test_runtime_registers_all_real_agents():
    rt = build_runtime()
    for cls in AGENT_CLASSES:
        assert rt.agent_registry.get(cls.agent_type) is not None
        assert cls.agent_type in rt.handlers
    assert len(rt.handlers) == 6


def test_worker_tick_end_to_end():
    rt = build_runtime()
    result = run_tick(rt, tick=1)
    assert result["ok"] is True
    assert result["opportunities"] > 0
    assert result["business_id"] is not None
    biz = rt.businesses.get(result["business_id"])
    assert biz is not None
    entries = rt.ledger.entries(biz.id)
    assert len(entries) == 1
    assert entries[0].amount_usd < 0  # AI spend recorded as a cost
    pnl = rt.ledger.pnl(biz.id)
    assert pnl.net_profit == entries[0].amount_usd


def test_dashboard_app_builds():
    from ecosystem.runtime import create_dashboard_app

    app = create_dashboard_app(build_runtime())
    paths = {r.path for r in app.routes}
    assert "/" in paths
    assert "/businesses" in paths
    assert "/businesses/{business_id}" in paths
    assert "/experiments" in paths
    assert "/approvals" in paths

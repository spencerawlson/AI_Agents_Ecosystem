"""Tests for the shared SQLite state store: same semantics as the
in-memory registry/ledger, but visible across instances (processes)
and durable across restarts — the fix for missions and the GUI living
in separate worlds."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ledger import Ledger
from core.models import BusinessStatus
from core.registry import InvalidTransition
from core.state_store import SharedBusinessRegistry, SharedLedger


def test_registry_semantics_match_memory(tmp_path):
    reg = SharedBusinessRegistry(tmp_path / "state.db")
    biz = reg.create("Farelune", "web_product", domain="farelune.com")
    assert reg.get(biz.id).name == "Farelune"
    assert [b.id for b in reg.list()] == [biz.id]
    assert reg.list(status=BusinessStatus.DISCOVERED)[0].id == biz.id
    moved = reg.transition(biz.id, BusinessStatus.RESEARCHING)
    assert moved.status == BusinessStatus.RESEARCHING
    with pytest.raises(InvalidTransition):
        reg.transition(biz.id, BusinessStatus.LAUNCHED)
    with pytest.raises(ValueError):
        reg.transition("biz_nope", BusinessStatus.RESEARCHING)
    killed = reg.transition(biz.id, BusinessStatus.TERMINATED)
    assert killed.status == BusinessStatus.TERMINATED


def test_businesses_are_shared_across_instances(tmp_path):
    db = tmp_path / "state.db"
    a = SharedBusinessRegistry(db)
    biz = a.create("ProofFlow", "web_product")
    a.transition(biz.id, BusinessStatus.RESEARCHING)
    # A second instance = a second process (dashboard vs mission).
    b = SharedBusinessRegistry(db)
    seen = b.get(biz.id)
    assert seen is not None and seen.status == BusinessStatus.RESEARCHING
    b.transition(biz.id, BusinessStatus.VALIDATED)
    assert a.get(biz.id).status == BusinessStatus.VALIDATED


def test_ledger_parity_and_sharing(tmp_path):
    db = tmp_path / "state.db"
    shared = SharedLedger(db)
    memory = Ledger()
    ops = [("revenue", 120.0), ("ai_spend", -3.25), ("external_cost", -40.0),
           ("advertising", -10.0), ("refund", -5.0)]
    for kind, amount in ops:
        shared.record("biz_1", kind, amount, f"{kind} entry")
        memory.record("biz_1", kind, amount, f"{kind} entry")
    sp, mp = shared.pnl("biz_1"), memory.pnl("biz_1")
    assert sp.revenue == mp.revenue == 120.0
    assert sp.total_costs == mp.total_costs
    assert sp.net_profit == mp.net_profit
    assert shared.capital_efficiency("biz_1") == \
        memory.capital_efficiency("biz_1")
    # A second instance sees the same books.
    other = SharedLedger(db)
    assert other.pnl("biz_1").net_profit == sp.net_profit
    assert len(other.entries("biz_1")) == len(ops)


def test_runtime_uses_shared_store_only_when_configured(tmp_path,
                                                        monkeypatch):
    from ecosystem.runtime import build_runtime

    db = str(tmp_path / "eco.db")
    monkeypatch.setenv("ECOSYSTEM_STATE_DB", db)
    rt1 = build_runtime()
    biz = rt1.businesses.create("Farelune", "web_product")
    rt1.ledger.record(biz.id, "ai_spend", -0.09, "growth run")
    rt2 = build_runtime()  # a second process's runtime
    assert rt2.businesses.get(biz.id) is not None
    assert rt2.ledger.pnl(biz.id).total_costs == pytest.approx(0.09)

    monkeypatch.delenv("ECOSYSTEM_STATE_DB")
    rt3 = build_runtime()
    rt4 = build_runtime()
    ghost = rt3.businesses.create("Ghost", "web_product")
    assert rt4.businesses.get(ghost.id) is None  # in-memory stays private

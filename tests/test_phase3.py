"""Phase 3 tests: allocation, portfolio analytics, shutdown."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.allocation import CapitalAllocator
from core.experiments import ExperimentEngine
from core.ledger import Ledger
from core.models import BusinessStatus
from core.portfolio import PortfolioAnalytics
from core.portfolio_experiments import PortfolioBudgetExceeded, PortfolioExperiments
from core.registry import BusinessRegistry
from core.shutdown import ShutdownWorkflow


def _walk_to_operating(registry: BusinessRegistry, biz_id: str) -> None:
    for status in (
        BusinessStatus.RESEARCHING, BusinessStatus.VALIDATED,
        BusinessStatus.APPROVED, BusinessStatus.BUILDING,
        BusinessStatus.TESTING, BusinessStatus.LAUNCHED,
        BusinessStatus.OPERATING,
    ):
        registry.transition(biz_id, status)


def _setup():
    registry = BusinessRegistry()
    ledger = Ledger()
    # Winner: high efficiency.
    w = registry.create("Winner", "ecommerce")
    _walk_to_operating(registry, w.id)
    ledger.record(w.id, "capital_deployed", -1000)
    ledger.record(w.id, "revenue", 3000)
    ledger.record(w.id, "cogs", -1000)
    # Break-even.
    m = registry.create("Middling", "ecommerce")
    _walk_to_operating(registry, m.id)
    ledger.record(m.id, "capital_deployed", -1000)
    ledger.record(m.id, "revenue", 1100)
    ledger.record(m.id, "cogs", -1000)
    # Loser.
    l = registry.create("Loser", "ecommerce")
    _walk_to_operating(registry, l.id)
    ledger.record(l.id, "capital_deployed", -1000)
    ledger.record(l.id, "revenue", 500)
    ledger.record(l.id, "cogs", -800)
    return registry, ledger, w, m, l


def test_allocation():
    registry, ledger, w, m, l = _setup()
    allocator = CapitalAllocator(registry, ledger)

    rec_w, _ = allocator.recommend(w.id)
    rec_l, _ = allocator.recommend(l.id)
    assert rec_w == "SCALE"
    assert rec_l in ("PAUSE", "SHUT_DOWN")

    allocs = allocator.allocate(10_000)
    by_id = {a.business_id: a for a in allocs}
    # Winner gets the most, loser gets nothing.
    assert by_id[w.id].allocated_usd > by_id[m.id].allocated_usd
    assert by_id[l.id].allocated_usd == 0.0
    # Sorted by efficiency desc.
    assert allocs[0].business_id == w.id

    # Beats naive equal-split: winner gets more than equal share.
    equal = allocator.equal_split(10_000)
    assert by_id[w.id].allocated_usd > equal[w.id]


def test_portfolio_analytics():
    registry, ledger, w, m, l = _setup()
    pa = PortfolioAnalytics(registry, ledger)
    bench = pa.benchmarks()
    assert bench["net_margin"]["best"] >= bench["net_margin"]["worst"]
    ranked = pa.rankings()
    assert ranked[0]["business_id"] == w.id
    assert ranked[-1]["business_id"] == l.id


def test_portfolio_experiments():
    engine = ExperimentEngine()
    pe = PortfolioExperiments(engine, portfolio_budget_usd=1000.0)
    e1 = pe.launch("biz_1", "hypothesis A", capital_usd=600)
    assert pe.committed == 600
    try:
        pe.launch("biz_2", "hypothesis B", capital_usd=500)
    except PortfolioBudgetExceeded:
        pass
    else:
        raise AssertionError("expected PortfolioBudgetExceeded")
    results = pe.evaluate_all()
    assert e1.id in results


def test_shutdown():
    registry, ledger, w, m, l = _setup()
    wf = ShutdownWorkflow(registry, ledger)
    report = wf.shutdown(l.id, lessons=["weak demand", "high CAC"])
    assert report.business_name == "Loser"
    assert report.final_net_profit == -1300  # revenue 500 - cogs 800 - capital 1000
    assert len(report.lessons) == 2
    assert registry.get(l.id).status == BusinessStatus.TERMINATED
    # Double shutdown rejected.
    try:
        wf.shutdown(l.id)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on double shutdown")


if __name__ == "__main__":
    test_allocation()
    test_portfolio_analytics()
    test_portfolio_experiments()
    test_shutdown()
    print("All Phase 3 tests passed.")

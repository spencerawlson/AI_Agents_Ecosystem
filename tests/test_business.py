"""Tests: registry state machine, scoring, experiments, ledger."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.experiments import ExperimentActuals, ExperimentEngine
from core.ledger import Ledger
from core.models import BusinessStatus, Opportunity
from core.registry import BusinessRegistry, InvalidTransition
from core.scoring import ScoringEngine


def test_registry_transitions():
    reg = BusinessRegistry()
    biz = reg.create("Test Store", "ecommerce")
    assert biz.status == BusinessStatus.DISCOVERED

    reg.transition(biz.id, BusinessStatus.RESEARCHING)
    reg.transition(biz.id, BusinessStatus.VALIDATED)
    assert reg.get(biz.id).status == BusinessStatus.VALIDATED

    try:
        reg.transition(biz.id, BusinessStatus.LAUNCHED)
    except InvalidTransition:
        pass
    else:
        raise AssertionError("expected InvalidTransition")

    # Termination allowed from non-terminal states.
    reg.transition(biz.id, BusinessStatus.TERMINATED)
    assert reg.get(biz.id).status == BusinessStatus.TERMINATED


def test_scoring_ranks():
    engine = ScoringEngine()
    good = Opportunity(
        niche="a", business_type="digital_product",
        demand_score=0.9, competition_score=0.2, expected_margin=0.8,
        startup_cost_usd=200, automation_potential=0.9, scalability=0.8,
    )
    bad = Opportunity(
        niche="b", business_type="ecommerce",
        demand_score=0.3, competition_score=0.9, expected_margin=0.2,
        startup_cost_usd=9000, operational_complexity=0.9,
        marketplace_risk=0.8,
    )
    ranked = engine.score_and_rank([bad, good])
    assert ranked[0].niche == "a"
    assert ranked[0].score > ranked[1].score
    assert 0 <= ranked[0].score <= 100


def test_experiment_scale():
    engine = ExperimentEngine()
    exp = engine.start(
        business_id="biz_1",
        initial_capital_usd=500,
        max_advertising_usd=300,
        duration_days=30,
        target_roas_min=2.5,
        target_conversion_min=0.025,
        max_refund_rate=0.05,
    )
    rec = engine.evaluate(
        exp.id,
        ExperimentActuals(
            revenue_usd=1940, operating_cost_usd=1330,
            advertising_usd=300, orders=48, visitors=1500,
            refunds=1, customers_acquired=45,
        ),
    )
    assert rec == "SCALE", f"expected SCALE, got {rec}"


def test_experiment_shutdown():
    engine = ExperimentEngine()
    exp = engine.start(
        business_id="biz_2",
        initial_capital_usd=500,
        max_advertising_usd=300,
        duration_days=30,
        target_roas_min=2.5,
    )
    rec = engine.evaluate(
        exp.id,
        ExperimentActuals(
            revenue_usd=120, operating_cost_usd=800,
            advertising_usd=300, orders=3, visitors=2000,
            refunds=2, customers_acquired=3,
        ),
    )
    assert rec == "SHUT DOWN", f"expected SHUT DOWN, got {rec}"


def test_ledger_pnl():
    ledger = Ledger()
    ledger.record("biz_1", "revenue", 4800)
    ledger.record("biz_1", "cogs", -1200)
    ledger.record("biz_1", "advertising", -900)
    ledger.record("biz_1", "ai_inference", -310)
    ledger.record("biz_1", "hosting", -300)

    pnl = ledger.pnl("biz_1")
    assert pnl.revenue == 4800
    assert pnl.total_costs == 2710
    assert pnl.net_profit == 2090
    assert round(pnl.net_margin, 4) == round(2090 / 4800, 4)

    eff = ledger.capital_efficiency("biz_1")
    assert round(eff, 4) == round(2090 / 2710, 4)


if __name__ == "__main__":
    test_registry_transitions()
    test_scoring_ranks()
    test_experiment_scale()
    test_experiment_shutdown()
    test_ledger_pnl()
    print("All business-domain tests passed.")

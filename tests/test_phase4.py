"""Phase 4 tests: memory, predictive, pricing, anomalies, routing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.marketing.agent import MarketingAgent
from core.intelligence import AnomalyDetector, ModelRouter
from core.memory import AgentMemory, MemoryType
from core.models import Opportunity
from core.optimization import MarketingOptimizer, PricingOptimizer
from core.predictive import PredictiveModel


def test_memory():
    mem = AgentMemory()
    mem.remember(MemoryType.FAILURE, "facebook ads flopped", business_id="b1",
                 outcome_score=-0.8, tags=["paid_social"])
    mem.remember(MemoryType.EXPERIMENT, "email worked", business_id="b1",
                 outcome_score=0.9, tags=["email"])
    assert len(mem.failures("b1")) == 1
    assert len(mem.successes("b1")) == 1
    mem.remember(MemoryType.SUPPLIER, "Acme Corp", outcome_score=0.6)
    mem.remember(MemoryType.SUPPLIER, "Acme Corp", outcome_score=0.8)
    assert mem.supplier_rating("acme") == 0.7
    assert mem.supplier_rating("unknown") is None


def test_predictive():
    mem = AgentMemory()
    mem.remember(MemoryType.EXPERIMENT, "notion templates won",
                 outcome_score=0.9, tags=["notion templates"])
    mem.remember(MemoryType.EXPERIMENT, "dropshipping lost",
                 outcome_score=-0.7, tags=["dropshipping"])
    model = PredictiveModel(mem)
    assert model.train() == 2

    good = Opportunity(niche="notion templates", business_type="digital",
                       demand_score=0.8, expected_margin=0.7)
    bad = Opportunity(niche="dropshipping", business_type="ecommerce",
                      demand_score=0.8, expected_margin=0.7)
    assert model.predict(good) > model.predict(bad)

    ranked = model.rank([bad, good])
    assert ranked[0][0].niche == "notion templates"


def test_pricing():
    mem = AgentMemory()
    opt = PricingOptimizer(mem)
    test = opt.start_test("b1", "prod_1", base_price=50.0, unit_cost=20.0, variants=3)
    assert len(test.price_points) == 3
    assert all(p > 20.0 for p in test.price_points)
    for price, rev in zip(test.price_points, [100.0, 180.0, 150.0]):
        opt.record_result(test.id, price, conversions=10, revenue=rev)
    winner = opt.pick_winner(test.id)
    assert test.results[winner]["revenue"] == 180.0


def test_roas_optimization():
    mkt = MarketingAgent()
    c1 = mkt.create_campaign("b1", "paid_search", "Search", budget_usd=1000)
    c2 = mkt.create_campaign("b1", "email", "Email", budget_usd=1000)
    mkt.spend(c1.id, 500)
    mkt.spend(c2.id, 500)
    opt = MarketingOptimizer(mkt)
    assert opt.roas(c1.id, 2000) == 4.0
    suggestions = opt.reallocate("b1", {c1.id: 2000, c2.id: 500})
    # Search (ROAS 4) should gain, email (ROAS 1) should lose.
    assert suggestions[c1.id] > 0
    assert suggestions[c2.id] < 0
    # Budget-neutral.
    assert abs(sum(suggestions.values())) < 0.01


def test_anomaly_detection():
    det = AnomalyDetector()
    for _ in range(10):
        assert det.observe("revenue", 100.0) is False
    # Sudden spike is anomalous.
    assert det.observe("revenue", 500.0) is True
    baseline = det.baseline("revenue")
    assert baseline is not None


def test_model_routing():
    router = ModelRouter()
    simple = router.route("summarize text", required_tier=1)
    complex_task = router.route("write strategy", required_tier=3)
    assert simple.name == "fast"
    assert complex_task.name == "powerful"
    router.log_spend("summarize text", simple, tokens=10_000)
    router.log_spend("write strategy", complex_task, tokens=10_000)
    assert router.total_ai_spend() > 0
    # Routing the simple task to fast saved money.
    assert router.savings_vs_always_powerful() > 0


if __name__ == "__main__":
    test_memory()
    test_predictive()
    test_pricing()
    test_roas_optimization()
    test_anomaly_detection()
    test_model_routing()
    print("All Phase 4 tests passed.")
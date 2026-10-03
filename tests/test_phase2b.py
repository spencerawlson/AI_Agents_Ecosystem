"""Phase 2b tests: operations, analytics, reporting, integrations."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.operations.agent import OperationsAgent, Severity
from core.analytics import Analytics
from core.integrations import AdapterRegistry, MockCommerceAdapter
from core.ledger import Ledger
from core.reporting import FinancialReporting
from orchestrator.models import Task


def test_operations_incidents():
    agent = OperationsAgent()
    # Healthy check raises nothing.
    assert agent.check_health("biz_1") == []
    # Website down -> critical.
    raised = agent.check_health("biz_1", website_up=False)
    assert len(raised) == 1 and raised[0].severity == Severity.CRITICAL
    # Payment failures breach threshold.
    raised = agent.check_health("biz_1", failed_payments_24h=7)
    assert any(i.category == "payment" for i in raised)
    assert len(agent.critical()) == 2
    # Resolve one.
    agent.resolve(raised[0].id)
    assert len(agent.critical()) == 1


def test_analytics_funnel():
    a = Analytics()
    for _ in range(100):
        a.track("biz_1", "pageview", session_id="s1")
    for _ in range(20):
        a.track("biz_1", "add_to_cart", session_id="s1")
    for _ in range(10):
        a.track("biz_1", "purchase", session_id="s1", customer_id="c1", value_usd=50.0)
    f = a.funnel("biz_1")
    assert f["pageview"] == 100 and f["purchase"] == 10
    assert a.conversion_rate("biz_1") == 0.1
    assert a.revenue("biz_1") == 500.0


def test_reporting():
    ledger = Ledger()
    ledger.record("biz_1", "revenue", 2000)
    ledger.record("biz_1", "cogs", -800)
    ledger.record("biz_1", "advertising", -400)
    rep = FinancialReporting(ledger)
    rep.record_cashflow("biz_1", 5000, "initial capital")
    rep.record_cashflow("biz_1", -1200, "expenses")
    assert rep.cash_position("biz_1") == 3800

    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = datetime(2026, 10, 31, tzinfo=timezone.utc)
    report = rep.profit_report("biz_1", start, end)
    assert report.net_profit == 800
    assert report.net_margin == 0.4

    summary = rep.portfolio_summary(["biz_1"])
    assert summary["total_net_profit"] == 800


def test_integrations():
    adapter = MockCommerceAdapter()
    prod = adapter.create_product({"name": "Widget", "price_usd": 29.0, "sku": "W1", "inventory": 100})
    assert prod["id"].startswith("prod_")
    order = adapter.simulate_order(prod["id"], quantity=2)
    assert order["total_usd"] == 58.0
    assert len(adapter.list_orders()) == 1
    inv = adapter.update_inventory("W1", 95)
    assert inv["quantity"] == 95

    registry = AdapterRegistry()
    registry.register_commerce("biz_1", adapter)
    assert registry.commerce("biz_1").platform == "mock"
    assert registry.commerce("nope") is None


def test_operations_report():
    agent = OperationsAgent()
    out = agent(Task(agent_type="operations", inputs={"action": "report"}))
    assert out["open_incidents"] == 0


if __name__ == "__main__":
    test_operations_incidents()
    test_analytics_funnel()
    test_reporting()
    test_integrations()
    test_operations_report()
    print("All Phase 2b tests passed.")
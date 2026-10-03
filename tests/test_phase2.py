"""Phase 2 agent tests: marketing budgets, support escalation, creative versioning."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.creative.agent import CreativeAgent
from agents.marketing.agent import BudgetExceededError, MarketingAgent
from agents.support.agent import SupportAgent, TicketStatus
from orchestrator.models import Task


def test_marketing_budget_cap():
    agent = MarketingAgent()
    camp = agent.create_campaign("biz_1", "paid_search", "Launch", budget_usd=300.0)
    agent.spend(camp.id, 100.0)
    assert agent._campaigns[camp.id].remaining == 200.0
    try:
        agent.spend(camp.id, 250.0)
    except BudgetExceededError:
        pass
    else:
        raise AssertionError("expected BudgetExceededError")
    # Budget intact after rejected spend.
    assert agent._campaigns[camp.id].spent_usd == 100.0


def test_marketing_report():
    agent = MarketingAgent()
    agent.create_campaign("biz_1", "email", "Welcome", budget_usd=50.0)
    out = agent(Task(agent_type="marketing", inputs={"action": "report"}))
    assert out["total_budget_usd"] == 50.0
    assert out["total_remaining_usd"] == 50.0


def test_support_escalation():
    agent = SupportAgent()
    # Routine ticket stays open.
    t1 = agent.open_ticket("biz_1", "cust_1", "Where is my order?", category="order")
    assert t1.status == TicketStatus.OPEN
    agent.resolve(t1.id, "Shipped yesterday, tracking sent.")
    assert agent._tickets[t1.id].status == TicketStatus.RESOLVED

    # Legal threat escalates.
    t2 = agent.open_ticket("biz_1", "cust_2", "My lawyer will contact you", category="complaint")
    assert t2.status == TicketStatus.ESCALATED
    assert "lawyer" in t2.escalated_reason

    # Large refund escalates.
    t3 = agent.open_ticket("biz_1", "cust_3", "Refund please", category="refund", amount_usd=250.0)
    assert t3.status == TicketStatus.ESCALATED

    # Small refund does not.
    t4 = agent.open_ticket("biz_1", "cust_4", "Refund please", category="refund", amount_usd=25.0)
    assert t4.status == TicketStatus.OPEN

    # Escalated tickets cannot be resolved by the agent.
    try:
        agent.resolve(t2.id, "nope")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError resolving escalated ticket")


def test_creative_versioning():
    agent = CreativeAgent()
    a1 = agent.create("biz_1", "copy", "Headline", "Buy now!", campaign_id="camp_0001")
    assert a1.version == 1
    a2 = agent.new_version(a1.id, "Buy now — 20% off!")
    assert a2.version == 2
    assert a2.id != a1.id  # immutable history
    # Original untouched.
    assert agent._assets[a1.id].content == "Buy now!"

    agent.record_performance(a2.id, impressions=1000, clicks=50, conversions=5)
    top = agent.top_performers("biz_1")
    assert top[0].id == a2.id


if __name__ == "__main__":
    test_marketing_budget_cap()
    test_marketing_report()
    test_support_escalation()
    test_creative_versioning()
    print("All Phase 2 agent tests passed.")
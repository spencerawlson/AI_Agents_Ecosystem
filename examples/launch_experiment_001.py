"""Experiment 001 launch: register the business, walk it to APPROVED, start Week 1.

Week 1 pipeline:
  - creative agent: 6 product briefs (first listings)
  - marketing agent: Etsy SEO keyword task
  - research agent: competitor pricing verification
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.creative.agent import CreativeAgent
from agents.marketing.agent import MarketingAgent
from agents.research.agent import ResearchAgent
from core.ledger import Ledger
from core.models import BusinessStatus, Opportunity
from core.registry import BusinessRegistry
from core.scoring import ScoringEngine
from orchestrator.engine import Orchestrator
from orchestrator.registry import AgentRegistry


def main() -> None:
    registry = BusinessRegistry()
    ledger = Ledger()

    # 1. The opportunity (from real Oct 2026 market research).
    opp = Opportunity(
        niche="etsy digital planners",
        business_type="digital",
        demand_score=0.8,
        competition_score=0.6,
        expected_margin=0.88,
        startup_cost_usd=50.0,
        advertising_cost_usd=50.0,
        operational_complexity=0.2,
        automation_potential=0.7,
        scalability=0.8,
        recurring_revenue=0.1,
        marketplace_risk=0.4,
        supplier_risk=0.1,
        support_burden=0.2,
    )
    score = ScoringEngine().score(opp)
    print(f"Opportunity scored: {score:.1f}/100")

    # 2. Register the business and walk the state machine to APPROVED.
    biz = registry.create("Evergreen Planners", "digital")
    for status in (
        BusinessStatus.RESEARCHING,
        BusinessStatus.VALIDATED,
        BusinessStatus.APPROVED,
    ):
        registry.transition(biz.id, status)
    print(f"Business {biz.id} ({biz.name}): {biz.status.value}")

    # 3. Seed the experiment budget in the ledger.
    ledger.record(biz.id, "capital_deployed", -91.0)
    print("Experiment budget seeded: $91 at risk")

    # 4. Wire up agents and submit Week 1 tasks.
    agent_registry = AgentRegistry()
    orch = Orchestrator(registry=agent_registry)
    creative = CreativeAgent()
    marketing = MarketingAgent()
    research = ResearchAgent()
    for agent in (creative, marketing, research):
        agent_registry.register(agent.agent_type, capabilities=agent.capabilities)
        orch.register_handler(agent.agent_type, agent)

    briefs = [
        ("ADHD Daily Planner", "Hyperlinked PDF daily planner for ADHD adults: time-blocking, brain-dump, priority matrix. $12."),
        ("ADHD Weekly Reset Planner", "Weekly review + reset system with habit tracker. $12."),
        ("Wedding Budget Suite", "Spreadsheet suite: budget tracker, vendor comparison, payment schedule. $15."),
        ("Wedding Timeline Planner", "12-month countdown checklist + day-of timeline. $9."),
        ("Content Calendar Kit", "Small-business 90-day content planner with caption prompts. $15."),
        ("Kids Chore Chart Bundle", "Printable chore charts + reward tracker, 5 designs. $8."),
    ]
    for title, desc in briefs:
        t = orch.submit(
            "creative",
            business_id=biz.id,
            inputs={"action": "brief", "title": title, "description": desc},
            budget_usd=2.0,
            budget_tokens=4000,
        )
        orch.dispatch(t.id)
        creative.create(biz.id, "copy", title, desc)
        print(f"  brief: {title}")

    t = orch.submit(
        "marketing", business_id=biz.id,
        inputs={"action": "seo_keywords", "niche": "etsy digital planners"},
        budget_usd=1.0, budget_tokens=2000,
    )
    orch.dispatch(t.id)

    t = orch.submit(
        "research", business_id=biz.id,
        inputs={"action": "verify_pricing", "niche": "etsy digital planners"},
        budget_usd=1.0, budget_tokens=2000,
    )
    orch.dispatch(t.id)

    print(f"\nWeek 1 pipeline dispatched: {len(orch.store.list_tasks())} tasks")
    print("Next: Spencer creates the Etsy shop; human publishes listings from briefs.")


if __name__ == "__main__":
    main()

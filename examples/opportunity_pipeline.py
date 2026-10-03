"""Full opportunity pipeline: discovery → research → scoring → registry.

Each stage runs as an orchestrated task. The winning opportunity
becomes a business in the registry.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.discovery.agent import DiscoveryAgent
from agents.research.agent import ResearchAgent
from core.models import BusinessStatus, Opportunity
from core.registry import BusinessRegistry
from core.scoring import ScoringEngine
from orchestrator.engine import Orchestrator
from orchestrator.models import TaskStatus
from orchestrator.registry import AgentRegistry


def main() -> None:
    agent_registry = AgentRegistry()
    for cls in (DiscoveryAgent, ResearchAgent):
        agent_registry.register(cls.agent_type, capabilities=cls.capabilities,
                                description=cls.description)
    orch = Orchestrator(registry=agent_registry)
    orch.register_handler("discovery", DiscoveryAgent())
    orch.register_handler("research", ResearchAgent())
    registry = BusinessRegistry()
    scorer = ScoringEngine()

    # 1. Discovery
    t1 = orch.submit("discovery", inputs={"limit": 5}, budget_usd=1.0, budget_tokens=5000)
    r1 = orch.dispatch(t1.id)
    assert r1.status == TaskStatus.COMPLETED, r1.error
    opportunities = r1.output["opportunities"]
    print(f"1. Discovered {len(opportunities)} opportunities.")

    # 2. Research
    t2 = orch.submit("research", inputs={"opportunities": opportunities},
                     budget_usd=2.0, budget_tokens=8000)
    r2 = orch.dispatch(t2.id)
    assert r2.status == TaskStatus.COMPLETED, r2.error
    reports = r2.output["reports"]
    print(f"2. Researched {len(reports)} opportunities.")
    for rep in reports:
        print(f"   - {rep['niche']}: {rep['verdict']} "
              f"(demand={rep['estimated_demand']}, competitors={rep['competitor_count']})")

    # 3. Score + rank (pursue verdicts only)
    pursued = [o for o, rep in zip(opportunities, reports) if rep["verdict"] == "pursue"]
    ranked = scorer.score_and_rank([Opportunity(**o) for o in pursued])
    print("3. Ranked:")
    for opp in ranked:
        print(f"   {opp.score:6.2f}  {opp.niche}")

    # 4. Winner → business
    winner = ranked[0]
    biz = registry.create(winner.niche, winner.business_type)
    registry.transition(biz.id, BusinessStatus.RESEARCHING)
    print(f"\n4. Created business: {biz.name} [{biz.id}] → {biz.status.value}")
    print("\nPipeline OK.")


if __name__ == "__main__":
    main()

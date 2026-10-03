"""End-to-end: discovery → scoring → registry.

Simulates the Phase 1 pipeline: a discovery task runs through the
orchestrator, opportunities are scored and ranked, and the winner
becomes a business in the registry.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.discovery.agent import DiscoveryAgent
from core.models import BusinessStatus, Opportunity
from core.registry import BusinessRegistry
from core.scoring import ScoringEngine
from orchestrator.engine import Orchestrator
from orchestrator.models import TaskStatus
from orchestrator.registry import AgentRegistry


def main() -> None:
    # Wire up.
    agent_registry = AgentRegistry()
    agent_registry.register(
        "discovery",
        capabilities=DiscoveryAgent.capabilities,
        description=DiscoveryAgent.description,
    )
    orch = Orchestrator(registry=agent_registry)
    orch.register_handler("discovery", DiscoveryAgent())
    registry = BusinessRegistry()
    scorer = ScoringEngine()

    # 1. Submit + dispatch a discovery task.
    task = orch.submit("discovery", inputs={"limit": 5, "market": "us"},
                       budget_usd=1.0, budget_tokens=5000)
    run = orch.dispatch(task.id)
    assert run.status == TaskStatus.COMPLETED, run.error
    raw_opps = run.output["opportunities"]
    print(f"Discovered {len(raw_opps)} opportunities.")

    # 2. Score and rank.
    opportunities = [Opportunity(**o) for o in raw_opps]
    ranked = scorer.score_and_rank(opportunities)
    for opp in ranked:
        print(f"  {opp.score:6.2f}  {opp.niche} ({opp.business_type})")

    # 3. Top opportunity becomes a business.
    winner = ranked[0]
    biz = registry.create(winner.niche, winner.business_type)
    print(f"\nCreated business: {biz.name} [{biz.id}] status={biz.status}")

    # 4. Move it into research.
    registry.transition(biz.id, BusinessStatus.RESEARCHING)
    print(f"Transitioned to: {registry.get(biz.id).status}")
    print("\nPipeline OK.")


if __name__ == "__main__":
    main()

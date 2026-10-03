"""Opportunity Discovery Agent.

Finds structured business opportunities. Data sources are pluggable —
the default heuristic source produces scored mock opportunities so the
pipeline works end to end; real sources (trend APIs, marketplace data)
implement the OpportunitySource protocol.
"""

from __future__ import annotations

from typing import Protocol

from agents.base import BaseAgent
from core.models import Opportunity
from orchestrator.models import Task


class OpportunitySource(Protocol):
    def fetch(self, inputs: dict) -> list[dict]: ...


class HeuristicSource:
    """Baseline source: curated niche templates with randomized variance.

    Replace with live sources (Google Trends, marketplace APIs) in Phase 2.
    """

    NICHES = [
        {"niche": "ergonomic desk accessories", "business_type": "ecommerce",
         "demand_score": 0.82, "competition_score": 0.55, "expected_margin": 0.45,
         "startup_cost_usd": 800, "automation_potential": 0.7, "scalability": 0.75},
        {"niche": "print-on-demand pet portraits", "business_type": "print_on_demand",
         "demand_score": 0.74, "competition_score": 0.62, "expected_margin": 0.55,
         "startup_cost_usd": 150, "automation_potential": 0.85, "scalability": 0.8},
        {"niche": "notion templates for freelancers", "business_type": "digital_product",
         "demand_score": 0.68, "competition_score": 0.48, "expected_margin": 0.92,
         "startup_cost_usd": 50, "automation_potential": 0.95, "scalability": 0.9,
         "recurring_revenue": 0.3},
        {"niche": "AI resume review service", "business_type": "ai_service",
         "demand_score": 0.77, "competition_score": 0.58, "expected_margin": 0.80,
         "startup_cost_usd": 300, "automation_potential": 0.9, "scalability": 0.85,
         "recurring_revenue": 0.6},
        {"niche": "local lead-gen for roofers", "business_type": "lead_generation",
         "demand_score": 0.71, "competition_score": 0.44, "expected_margin": 0.65,
         "startup_cost_usd": 500, "automation_potential": 0.6, "scalability": 0.7,
         "recurring_revenue": 0.8},
    ]

    def fetch(self, inputs: dict) -> list[dict]:
        limit = inputs.get("limit", 5)
        return [dict(n) for n in self.NICHES[:limit]]


class DiscoveryAgent(BaseAgent):
    agent_type = "discovery"
    capabilities = [
        "niche_discovery",
        "trend_detection",
        "product_discovery",
        "marketplace_research",
    ]
    description = "Finds structured business opportunities."

    def __init__(self, source: OpportunitySource | None = None) -> None:
        super().__init__()
        self.source = source or HeuristicSource()

    def run(self, task: Task) -> dict:
        raw = self.source.fetch(task.inputs)
        self.record_usage(task, tokens=200 * len(raw), cost_usd=0.01 * len(raw))
        opportunities = [Opportunity(**item).model_dump() for item in raw]
        return {"opportunities": opportunities, "count": len(opportunities)}

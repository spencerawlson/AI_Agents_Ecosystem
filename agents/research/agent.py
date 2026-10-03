"""Market Research Agent.

Performs deeper analysis on opportunities from the Discovery Agent:
market demand, competitors, pricing, customer profiles, suppliers,
advertising competition, fees, margins, barriers, legal constraints,
differentiation. Produces a standardized opportunity report.
"""

from __future__ import annotations

from typing import Protocol

from agents.base import BaseAgent
from orchestrator.models import Task
from pydantic import BaseModel, Field


class OpportunityReport(BaseModel):
    """Standardized research output for one opportunity."""

    opportunity_id: str
    niche: str
    business_type: str
    estimated_demand: str = Field(description="low | medium | high")
    competitor_count: int = 0
    top_competitors: list[str] = Field(default_factory=list)
    price_range_usd: tuple[float, float] = (0.0, 0.0)
    customer_profile: str = ""
    advertising_competition: str = Field(description="low | medium | high")
    marketplace_fees_pct: float = 0.0
    expected_margin: float = 0.0
    barriers_to_entry: list[str] = Field(default_factory=list)
    legal_constraints: list[str] = Field(default_factory=list)
    differentiation_angles: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    verdict: str = Field(description="pursue | watch | reject")


class ResearchSource(Protocol):
    def research(self, opportunity: dict) -> dict: ...


class HeuristicResearchSource:
    """Baseline: deterministic enrichment from niche keywords.

    Replace with live sources (search APIs, marketplace scrapers) in Phase 2.
    """

    def research(self, opportunity: dict) -> dict:
        niche = opportunity.get("niche", "")
        btype = opportunity.get("business_type", "")
        # Deterministic pseudo-analysis keyed on niche hash.
        h = abs(hash(niche)) % 100
        return {
            "opportunity_id": opportunity.get("id", ""),
            "niche": niche,
            "business_type": btype,
            "estimated_demand": "high" if h > 60 else "medium" if h > 30 else "low",
            "competitor_count": 5 + (h % 20),
            "top_competitors": [f"competitor-{i}" for i in range(1, 4)],
            "price_range_usd": (19.0, 99.0),
            "customer_profile": f"online buyers interested in {niche}",
            "advertising_competition": "medium",
            "marketplace_fees_pct": 0.08,
            "expected_margin": opportunity.get("expected_margin", 0.5),
            "barriers_to_entry": ["brand trust", "ad creative testing"],
            "legal_constraints": [],
            "differentiation_angles": ["niche specialization", "bundle pricing"],
            "risks": ["platform dependence"] if "etsy" in niche.lower() else [],
            "verdict": "pursue" if h > 40 else "watch",
        }


class ResearchAgent(BaseAgent):
    agent_type = "research"
    capabilities = [
        "market_research",
        "competitor_analysis",
        "demand_estimation",
        "opportunity_report",
    ]
    description = "Deep research on discovered opportunities."

    def __init__(self, source: ResearchSource | None = None) -> None:
        super().__init__()
        self.source = source or HeuristicResearchSource()

    def run(self, task: Task) -> dict:
        opportunities = task.inputs.get("opportunities", [])
        reports = []
        for opp in opportunities:
            raw = self.source.research(opp)
            report = self.validate_output(raw, OpportunityReport)
            reports.append(report.model_dump())
        self.record_usage(task, tokens=800 * len(reports), cost_usd=0.03 * len(reports))
        return {"reports": reports, "count": len(reports)}
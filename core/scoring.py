"""Opportunity scoring engine.

Scores opportunities 0..100 from configurable weighted factors.
Higher is better. Cost/risk factors are inverted so that lower
cost/risk always increases the score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Opportunity


@dataclass
class ScoringWeights:
    demand: float = 0.20
    competition: float = 0.12          # inverted: less competition = higher score
    expected_margin: float = 0.15
    startup_cost: float = 0.08         # inverted
    advertising_cost: float = 0.06     # inverted
    operational_complexity: float = 0.06  # inverted
    automation_potential: float = 0.10
    scalability: float = 0.08
    recurring_revenue: float = 0.07
    marketplace_risk: float = 0.03     # inverted
    supplier_risk: float = 0.02        # inverted
    support_burden: float = 0.03       # inverted

    def as_dict(self) -> dict[str, float]:
        return {k: v for k, v in self.__dict__.items()}


# Factors where a lower raw value is better (inverted: 1 - value).
INVERTED = {
    "competition",
    "startup_cost",
    "advertising_cost",
    "operational_complexity",
    "marketplace_risk",
    "supplier_risk",
    "support_burden",
}

# Reference maxima used to normalize dollar amounts to 0..1.
REF_MAX_STARTUP_COST = 10_000.0
REF_MAX_ADVERTISING_COST = 5_000.0


class ScoringEngine:
    def __init__(self, weights: ScoringWeights | None = None) -> None:
        self.weights = weights or ScoringWeights()

    def _factors(self, opp: Opportunity) -> dict[str, float]:
        return {
            "demand": opp.demand_score,
            "competition": opp.competition_score,
            "expected_margin": opp.expected_margin,
            "startup_cost": min(opp.startup_cost_usd / REF_MAX_STARTUP_COST, 1.0),
            "advertising_cost": min(opp.advertising_cost_usd / REF_MAX_ADVERTISING_COST, 1.0),
            "operational_complexity": opp.operational_complexity,
            "automation_potential": opp.automation_potential,
            "scalability": opp.scalability,
            "recurring_revenue": opp.recurring_revenue,
            "marketplace_risk": opp.marketplace_risk,
            "supplier_risk": opp.supplier_risk,
            "support_burden": opp.support_burden,
        }

    def score(self, opp: Opportunity) -> float:
        factors = self._factors(opp)
        total = 0.0
        for name, weight in self.weights.as_dict().items():
            value = factors[name]
            if name in INVERTED:
                value = 1.0 - value
            total += weight * max(0.0, min(1.0, value))
        return round(total * 100, 2)

    def score_and_rank(self, opportunities: list[Opportunity]) -> list[Opportunity]:
        for opp in opportunities:
            opp.score = self.score(opp)
        return sorted(opportunities, key=lambda o: o.score or 0.0, reverse=True)

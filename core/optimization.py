"""Dynamic pricing experiments and ROAS-based marketing optimization.

Pricing: test price points within guardrails, measure conversion
and margin, converge toward the revenue-maximizing price.

Marketing optimization: reallocate budget toward campaigns with
the highest measured ROAS (return on ad spend).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agents.marketing.agent import MarketingAgent
from core.memory import AgentMemory, MemoryType
from orchestrator.models import new_id


class PriceTest(BaseModel):
    id: str = Field(default_factory=lambda: new_id("price"))
    business_id: str
    product_id: str
    price_points: list[float]
    results: dict[float, dict] = Field(default_factory=dict)  # price -> {conversions, revenue}
    winner: float | None = None


class PricingOptimizer:
    # Guardrails: never price below cost, never above 3x base.
    MAX_MULTIPLIER = 3.0

    def __init__(self, memory: AgentMemory | None = None) -> None:
        self.memory = memory or AgentMemory()
        self._tests: dict[str, PriceTest] = {}

    def start_test(
        self,
        business_id: str,
        product_id: str,
        base_price: float,
        unit_cost: float,
        variants: int = 3,
    ) -> PriceTest:
        if base_price <= unit_cost:
            raise ValueError("base price must exceed unit cost")
        step = base_price * 0.1
        points = [round(base_price + (i - variants // 2) * step, 2)
                  for i in range(variants)]
        points = [p for p in points
                  if unit_cost < p <= base_price * self.MAX_MULTIPLIER]
        test = PriceTest(
            business_id=business_id,
            product_id=product_id,
            price_points=points,
        )
        self._tests[test.id] = test
        return test

    def record_result(self, test_id: str, price: float, conversions: int, revenue: float) -> PriceTest:
        test = self._tests.get(test_id)
        if test is None:
            raise ValueError(f"unknown price test: {test_id}")
        test.results[price] = {"conversions": conversions, "revenue": revenue}
        return test

    def pick_winner(self, test_id: str) -> float:
        test = self._tests.get(test_id)
        if test is None:
            raise ValueError(f"unknown price test: {test_id}")
        if not test.results:
            raise ValueError("no results recorded yet")
        winner = max(test.results, key=lambda p: test.results[p]["revenue"])
        test.winner = winner
        self.memory.remember(
            MemoryType.PRICING,
            title=f"price test winner: ${winner}",
            business_id=test.business_id,
            outcome_score=0.5,
            tags=[test.product_id],
        )
        return winner


class MarketingOptimizer:
    def __init__(self, marketing: MarketingAgent | None = None) -> None:
        self.marketing = marketing or MarketingAgent()

    def roas(self, campaign_id: str, revenue_usd: float) -> float:
        """Return on ad spend: revenue / spend."""
        camp = self.marketing._campaigns.get(campaign_id)
        if camp is None:
            raise ValueError(f"unknown campaign: {campaign_id}")
        if camp.spent_usd == 0:
            return 0.0
        return revenue_usd / camp.spent_usd

    def reallocate(self, business_id: str, revenue_by_campaign: dict[str, float]) -> dict[str, float]:
        """Suggest budget shifts toward highest-ROAS campaigns.

        Returns {campaign_id: suggested_budget_delta}. Positive means
        increase, negative means decrease. Total stays constant.
        """
        campaigns = [c for c in self.marketing._campaigns.values()
                     if c.business_id == business_id and c.status == "active"]
        if len(campaigns) < 2:
            return {}
        roas = {c.id: self.roas(c.id, revenue_by_campaign.get(c.id, 0.0))
                for c in campaigns}
        avg_roas = sum(roas.values()) / len(roas)
        total_budget = sum(c.budget_usd for c in campaigns)
        suggestions: dict[str, float] = {}
        for c in campaigns:
            # Shift 20% of budget from below-average to above-average.
            delta = 0.2 * c.budget_usd * (1 if roas[c.id] > avg_roas else -1)
            suggestions[c.id] = round(delta, 2)
        # Normalize so total delta is zero (budget-neutral).
        total_delta = sum(suggestions.values())
        if suggestions:
            first = next(iter(suggestions))
            suggestions[first] = round(suggestions[first] - total_delta, 2)
        return suggestions
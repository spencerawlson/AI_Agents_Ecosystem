"""LLM-backed research source (cheap tier, fallback-safe).

Produces 12-factor scoring evidence for an opportunity via the real
LLM gateway. On any LLM failure, delegates to HeuristicResearchSource
so the pipeline keeps running — never crashes a tick.
"""

from __future__ import annotations

import json
from typing import Any

from agents.research.agent import HeuristicResearchSource
from core.llm import LLMGateway, LLMUnavailable

_SYSTEM = (
    "You are a market research analyst for an autonomous "
    "business-building AI. You produce honest, grounded assessments — "
    "flag weak ideas as reject, don't hype everything."
)


class LLMResearchSource:
    """ResearchSource backed by the cheap-tier LLM."""

    def __init__(self, gateway: LLMGateway | None = None,
                 fallback: Any | None = None) -> None:
        self.gateway = gateway or LLMGateway()
        self.fallback = fallback or HeuristicResearchSource()
        self.last_usage: dict | None = None  # {"tokens": int, "cost_usd": float}

    def research(self, opportunity: dict) -> dict:
        try:
            result = self.gateway.complete(
                prompt=(
                    "Research this micro-business opportunity and return "
                    "STRICT JSON only, no markdown, no commentary. Schema: "
                    '{"opportunity_id": str, "niche": str, "business_type": str, '
                    '"estimated_demand": "low"|"medium"|"high", '
                    '"competitor_count": int, "top_competitors": [str], '
                    '"price_range_usd": [low, high], '
                    '"customer_profile": str, '
                    '"advertising_competition": "low"|"medium"|"high", '
                    '"marketplace_fees_pct": 0..1, "expected_margin": 0..1, '
                    '"barriers_to_entry": [str], "legal_constraints": [str], '
                    '"differentiation_angles": [str], "risks": [str], '
                    '"verdict": "pursue"|"watch"|"reject"}. '
                    "Your evidence must cover the 12 scoring factors: "
                    "demand, competition, expected_margin, startup_cost, "
                    "advertising_cost, operational_complexity, "
                    "automation_potential, scalability, recurring_revenue, "
                    "marketplace_risk, supplier_risk, support_burden. "
                    "Set verdict to reject for weak ideas. Opportunity: "
                    + json.dumps(opportunity, default=str)[:2000]
                ),
                tier=LLMGateway.CHEAP,
                system=_SYSTEM,
                json_mode=True,
            )
        except LLMUnavailable:
            self.last_usage = None
            return self.fallback.research(opportunity)

        data = result["json"] or {}
        tokens = result["input_tokens"] + result["output_tokens"]
        if self.last_usage is None:
            self.last_usage = {"tokens": 0, "cost_usd": 0.0}
        self.last_usage["tokens"] += tokens
        self.last_usage["cost_usd"] += result["cost_usd"]

        data.setdefault("opportunity_id", opportunity.get("id", ""))
        data.setdefault("niche", opportunity.get("niche", ""))
        data.setdefault("business_type", opportunity.get("business_type", ""))
        return data

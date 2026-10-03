"""LLM-backed opportunity source (cheap tier, fallback-safe).

Proposes micro-business opportunities via the real LLM gateway.
On any LLM failure, delegates to HeuristicSource so the pipeline
keeps running — never crashes a tick.
"""

from __future__ import annotations

from typing import Any

from agents.discovery.agent import HeuristicSource
from core.llm import LLMGateway, LLMUnavailable

# The 12 scoring-engine factors the LLM must evidence in its output.
FACTOR_NAMES = [
    "demand", "competition", "expected_margin", "startup_cost",
    "advertising_cost", "operational_complexity", "automation_potential",
    "scalability", "recurring_revenue", "marketplace_risk",
    "supplier_risk", "support_burden",
]

_SYSTEM = (
    "You are a micro-business opportunity scout for an autonomous "
    "business-building AI. You propose small online businesses a solo "
    "operator could realistically launch and run."
)


def _clamp01(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _sanitize(raw: dict) -> dict:
    """Coerce one LLM opportunity dict onto the Opportunity schema."""
    score_fields = [
        "demand_score", "competition_score", "expected_margin",
        "operational_complexity", "automation_potential", "scalability",
        "recurring_revenue", "marketplace_risk", "supplier_risk",
        "support_burden",
    ]
    out = {
        "niche": str(raw.get("niche") or raw.get("name") or "untitled niche"),
        "business_type": str(raw.get("business_type") or "digital_product"),
    }
    for field in score_fields:
        out[field] = _clamp01(raw.get(field))
    for field in ("startup_cost_usd", "advertising_cost_usd"):
        try:
            out[field] = max(0.0, float(raw.get(field, 0.0)))
        except (TypeError, ValueError):
            out[field] = 0.0
    try:
        out["time_to_market_days"] = max(0, int(raw.get("time_to_market_days", 30)))
    except (TypeError, ValueError):
        out["time_to_market_days"] = 30
    return out


class LLMOpportunitySource:
    """OpportunitySource backed by the cheap-tier LLM."""

    def __init__(self, gateway: LLMGateway | None = None,
                 fallback: Any | None = None) -> None:
        self.gateway = gateway or LLMGateway()
        self.fallback = fallback or HeuristicSource()
        self.last_usage: dict | None = None  # {"tokens": int, "cost_usd": float}

    def fetch(self, inputs: dict) -> list[dict]:
        limit = inputs.get("limit", 5)
        try:
            result = self.gateway.complete(
                prompt=(
                    f"Propose {limit} micro-business opportunities suitable "
                    "for a solo founder in October 2026. Favor digital "
                    "products, AI services, and low-startup-cost ecommerce. "
                    "Respond with STRICT JSON only, no markdown, no commentary: "
                    '{"opportunities": [{"niche": str, '
                    '"business_type": str (one of: ecommerce, print_on_demand, '
                    "digital_product, ai_service, lead_generation), "
                    '"one_liner": str, "why_now": str, '
                    '"demand_score": 0..1, '
                    '"competition_score": 0..1 (higher = more competitive), '
                    '"expected_margin": 0..1, "startup_cost_usd": number, '
                    '"advertising_cost_usd": number, '
                    '"operational_complexity": 0..1, '
                    '"automation_potential": 0..1, "scalability": 0..1, '
                    '"recurring_revenue": 0..1, "marketplace_risk": 0..1, '
                    '"supplier_risk": 0..1, "support_burden": 0..1, '
                    '"time_to_market_days": int}]}. '
                    "The 12 scoring factors are: "
                    + ", ".join(FACTOR_NAMES)
                    + ". Score honestly — not everything should be high."
                ),
                tier=LLMGateway.CHEAP,
                system=_SYSTEM,
                json_mode=True,
            )
        except LLMUnavailable:
            self.last_usage = None
            return self.fallback.fetch(inputs)

        data = result["json"] or {}
        opps = data.get("opportunities") or []
        self.last_usage = {
            "tokens": result["input_tokens"] + result["output_tokens"],
            "cost_usd": result["cost_usd"],
        }
        return [_sanitize(o) for o in opps[:limit] if isinstance(o, dict)]

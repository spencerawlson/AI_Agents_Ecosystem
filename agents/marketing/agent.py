"""Marketing Agent: customer acquisition within hard budget caps.

Channels: SEO, paid search/social, email, content, affiliates.
Every campaign has a fixed budget. The agent CANNOT exceed it —
spend requests beyond the cap require human approval via the
orchestrator's approval gate.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agents.base import BaseAgent
from orchestrator.models import Task


class Campaign(BaseModel):
    id: str
    business_id: str
    channel: str  # seo | paid_search | paid_social | email | content | affiliate
    name: str
    budget_usd: float
    spent_usd: float = 0.0
    status: str = "active"  # active | paused | completed

    @property
    def remaining(self) -> float:
        return max(0.0, self.budget_usd - self.spent_usd)


class BudgetExceededError(RuntimeError):
    pass


class MarketingAgent(BaseAgent):
    agent_type = "marketing"
    capabilities = [
        "campaign_management",
        "seo",
        "paid_advertising",
        "email_marketing",
        "content_marketing",
    ]
    description = "Customer acquisition within hard budget caps."

    def __init__(self) -> None:
        super().__init__()
        self._campaigns: dict[str, Campaign] = {}

    def create_campaign(
        self, business_id: str, channel: str, name: str, budget_usd: float
    ) -> Campaign:
        if budget_usd <= 0:
            raise ValueError("campaign budget must be positive")
        camp = Campaign(
            id=f"camp_{len(self._campaigns) + 1:04d}",
            business_id=business_id,
            channel=channel,
            name=name,
            budget_usd=budget_usd,
        )
        self._campaigns[camp.id] = camp
        return camp

    def spend(self, campaign_id: str, amount_usd: float) -> Campaign:
        """Record spend. Raises if it would exceed the campaign budget."""
        camp = self._campaigns.get(campaign_id)
        if camp is None:
            raise ValueError(f"unknown campaign: {campaign_id}")
        if camp.status != "active":
            raise ValueError(f"campaign {campaign_id} is not active")
        if amount_usd < 0:
            raise ValueError("spend amount cannot be negative")
        if camp.spent_usd + amount_usd > camp.budget_usd:
            raise BudgetExceededError(
                f"campaign {campaign_id}: ${camp.spent_usd + amount_usd:.2f} "
                f"would exceed ${camp.budget_usd:.2f} budget "
                f"(${camp.remaining:.2f} remaining)"
            )
        camp.spent_usd += amount_usd
        return camp

    def pause_campaign(self, campaign_id: str) -> Campaign:
        camp = self._campaigns.get(campaign_id)
        if camp is None:
            raise ValueError(f"unknown campaign: {campaign_id}")
        camp.status = "paused"
        return camp

    def run(self, task: Task) -> dict:
        action = task.inputs.get("action", "report")
        if action == "report":
            campaigns = [c.model_dump() for c in self._campaigns.values()]
            total_budget = sum(c.budget_usd for c in self._campaigns.values())
            total_spent = sum(c.spent_usd for c in self._campaigns.values())
            self.record_usage(task, tokens=300, cost_usd=0.01)
            return {
                "campaigns": campaigns,
                "total_budget_usd": total_budget,
                "total_spent_usd": total_spent,
                "total_remaining_usd": total_budget - total_spent,
            }
        raise ValueError(f"unknown action: {action}")

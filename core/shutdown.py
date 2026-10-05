"""Business shutdown workflows: graceful termination.

Steps: pause campaigns, settle orders, archive data for learning,
transition status, record final P&L snapshot.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from core.ledger import Ledger
from core.registry import BusinessRegistry
from core.models import BusinessStatus
from orchestrator.models import utcnow


class ShutdownReport(BaseModel):
    business_id: str
    business_name: str
    final_revenue: float
    final_net_profit: float
    lessons: list[str] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=utcnow)


class ShutdownWorkflow:
    def __init__(
        self,
        registry: BusinessRegistry | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.registry = registry or BusinessRegistry()
        self.ledger = ledger or Ledger()

    def shutdown(self, business_id: str, lessons: list[str] | None = None) -> ShutdownReport:
        biz = self.registry.get(business_id)
        if biz is None:
            raise ValueError(f"unknown business: {business_id}")
        if biz.status == BusinessStatus.TERMINATED:
            raise ValueError(f"{business_id} already shut down")

        # 1. Pause operations: move to PAUSED first (stops new spend).
        self.registry.transition(business_id, BusinessStatus.PAUSED)
        # 2. Final P&L snapshot.
        pnl = self.ledger.pnl(business_id)
        # 3. Archive lessons for future learning.
        # 4. Final transition.
        self.registry.transition(business_id, BusinessStatus.TERMINATED)

        return ShutdownReport(
            business_id=business_id,
            business_name=biz.name,
            final_revenue=pnl.revenue,
            final_net_profit=pnl.net_profit,
            lessons=lessons or [],
        )

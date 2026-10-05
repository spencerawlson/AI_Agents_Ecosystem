"""Financial reporting: scheduled P&L summaries and cash-flow views."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from core.ledger import Ledger
from orchestrator.models import new_id, utcnow


class ProfitReport(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rpt"))
    business_id: str
    period_start: datetime
    period_end: datetime
    revenue: float
    total_costs: float
    net_profit: float
    gross_margin: float
    net_margin: float
    capital_efficiency: float
    generated_at: datetime = Field(default_factory=utcnow)


class CashFlowEntry(BaseModel):
    business_id: str
    amount_usd: float  # positive = inflow, negative = outflow
    description: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class FinancialReporting:
    def __init__(self, ledger: Ledger | None = None) -> None:
        self.ledger = ledger or Ledger()
        self._cashflow: list[CashFlowEntry] = []

    def record_cashflow(
        self, business_id: str, amount_usd: float, description: str = ""
    ) -> CashFlowEntry:
        entry = CashFlowEntry(
            business_id=business_id, amount_usd=amount_usd, description=description
        )
        self._cashflow.append(entry)
        return entry

    def cash_position(self, business_id: str) -> float:
        return sum(
            e.amount_usd for e in self._cashflow if e.business_id == business_id
        )

    def profit_report(
        self,
        business_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> ProfitReport:
        pnl = self.ledger.pnl(business_id)
        return ProfitReport(
            business_id=business_id,
            period_start=period_start,
            period_end=period_end,
            revenue=pnl.revenue,
            total_costs=pnl.total_costs,
            net_profit=pnl.net_profit,
            gross_margin=pnl.gross_margin,
            net_margin=pnl.net_margin,
            capital_efficiency=self.ledger.capital_efficiency(business_id),
        )

    def portfolio_summary(self, business_ids: list[str]) -> dict:
        total_revenue = 0.0
        total_profit = 0.0
        for bid in business_ids:
            pnl = self.ledger.pnl(bid)
            total_revenue += pnl.revenue
            total_profit += pnl.net_profit
        return {
            "businesses": len(business_ids),
            "total_revenue": total_revenue,
            "total_net_profit": total_profit,
            "blended_margin": total_profit / total_revenue if total_revenue else 0.0,
        }

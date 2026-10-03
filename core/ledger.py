"""Financial ledger: per-business P&L and unit economics.

Revenue alone never determines success — contribution profit does.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import LedgerEntry


@dataclass
class ProfitAndLoss:
    revenue: float = 0.0
    cogs: float = 0.0
    shipping: float = 0.0
    marketplace_fees: float = 0.0
    payment_processing: float = 0.0
    advertising: float = 0.0
    refunds: float = 0.0
    chargebacks: float = 0.0
    hosting: float = 0.0
    api_cost: float = 0.0
    ai_inference: float = 0.0
    subscriptions: float = 0.0
    other: float = 0.0

    @property
    def total_costs(self) -> float:
        return (
            self.cogs + self.shipping + self.marketplace_fees
            + self.payment_processing + self.advertising + self.refunds
            + self.chargebacks + self.hosting + self.api_cost
            + self.ai_inference + self.subscriptions + self.other
        )

    @property
    def net_profit(self) -> float:
        return self.revenue - self.total_costs

    @property
    def gross_margin(self) -> float:
        return (self.revenue - self.cogs) / self.revenue if self.revenue else 0.0

    @property
    def net_margin(self) -> float:
        return self.net_profit / self.revenue if self.revenue else 0.0


# Maps ledger entry kinds to P&L fields. Unknown kinds land in `other`.
KIND_TO_FIELD = {
    "revenue": "revenue",
    "cogs": "cogs",
    "shipping": "shipping",
    "marketplace_fee": "marketplace_fees",
    "payment_processing": "payment_processing",
    "advertising": "advertising",
    "refund": "refunds",
    "chargeback": "chargebacks",
    "hosting": "hosting",
    "api_cost": "api_cost",
    "ai_inference": "ai_inference",
    "subscription": "subscriptions",
}


class Ledger:
    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def record(
        self,
        business_id: str,
        kind: str,
        amount_usd: float,
        description: str = "",
    ) -> LedgerEntry:
        """Record a line item. Revenue is positive, costs are negative."""
        entry = LedgerEntry(
            business_id=business_id,
            kind=kind,
            amount_usd=amount_usd,
            description=description,
        )
        self._entries.append(entry)
        return entry

    def entries(self, business_id: str) -> list[LedgerEntry]:
        return [e for e in self._entries if e.business_id == business_id]

    def pnl(self, business_id: str) -> ProfitAndLoss:
        pnl = ProfitAndLoss()
        for entry in self.entries(business_id):
            field = KIND_TO_FIELD.get(entry.kind, "other")
            # Amounts accumulate as positives; net_profit = revenue - costs.
            setattr(pnl, field, getattr(pnl, field) + abs(entry.amount_usd))
        return pnl

    def capital_deployed(self, business_id: str) -> float:
        """Total costs invested — denominator for capital efficiency."""
        return self.pnl(business_id).total_costs

    def capital_efficiency(self, business_id: str) -> float:
        deployed = self.capital_deployed(business_id)
        if deployed <= 0:
            return 0.0
        return self.pnl(business_id).net_profit / deployed
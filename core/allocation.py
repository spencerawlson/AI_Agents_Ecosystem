"""Capital allocation engine: rank businesses, recommend actions.

Allocates a fixed pool of capital across businesses by capital
efficiency (net profit / capital deployed). Recommendations:
SCALE / MAINTAIN / REDUCE / PAUSE / SHUT DOWN.

Demonstrably beats naive equal-split: allocation is proportional
to efficiency (winners get more, losers get cut).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.ledger import Ledger
from core.registry import BusinessRegistry
from orchestrator.models import new_id


class Allocation(BaseModel):
    business_id: str
    capital_efficiency: float
    recommendation: str  # SCALE | MAINTAIN | REDUCE | PAUSE | SHUT_DOWN
    allocated_usd: float
    reasoning: str = ""


class CapitalAllocator:
    def __init__(
        self,
        registry: BusinessRegistry | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.registry = registry or BusinessRegistry()
        self.ledger = ledger or Ledger()

    def efficiency(self, business_id: str) -> float:
        return self.ledger.capital_efficiency(business_id)

    def recommend(self, business_id: str) -> tuple[str, str]:
        """(recommendation, reasoning) for one business."""
        eff = self.efficiency(business_id)
        pnl = self.ledger.pnl(business_id)
        if pnl.net_profit < 0 and eff < -0.2:
            return ("SHUT_DOWN", f"net ${pnl.net_profit:,.0f}, efficiency {eff:.2f}")
        if eff < 0:
            return ("PAUSE", f"losing money, efficiency {eff:.2f}")
        if eff < 0.1:
            return ("REDUCE", f"weak returns, efficiency {eff:.2f}")
        if eff < 0.3:
            return ("MAINTAIN", f"acceptable returns, efficiency {eff:.2f}")
        return ("SCALE", f"strong returns, efficiency {eff:.2f}")

    def allocate(
        self, total_capital_usd: float, minimum_usd: float = 100.0
    ) -> list[Allocation]:
        """Allocate capital proportional to efficiency (floored at minimum).

        Businesses recommended SHUT_DOWN / PAUSE get $0.
        """
        businesses = self.registry.list()
        scored: list[tuple[str, float, str, str]] = []
        for biz in businesses:
            rec, reason = self.recommend(biz.id)
            eff = max(0.0, self.efficiency(biz.id))
            scored.append((biz.id, eff, rec, reason))

        fundable = [(bid, eff, rec, reason) for bid, eff, rec, reason in scored
                    if rec not in ("SHUT_DOWN", "PAUSE")]
        total_eff = sum(eff for _, eff, _, _ in fundable)

        allocations: list[Allocation] = []
        for bid, eff, rec, reason in scored:
            if rec in ("SHUT_DOWN", "PAUSE"):
                allocations.append(Allocation(
                    business_id=bid, capital_efficiency=self.efficiency(bid),
                    recommendation=rec, allocated_usd=0.0, reasoning=reason,
                ))
            elif total_eff > 0:
                share = eff / total_eff
                amount = max(minimum_usd, total_capital_usd * share)
                allocations.append(Allocation(
                    business_id=bid, capital_efficiency=self.efficiency(bid),
                    recommendation=rec, allocated_usd=round(amount, 2),
                    reasoning=reason,
                ))
            else:
                # No positive efficiency: equal minimum to MAINTAIN only.
                amount = minimum_usd if rec == "MAINTAIN" else 0.0
                allocations.append(Allocation(
                    business_id=bid, capital_efficiency=self.efficiency(bid),
                    recommendation=rec, allocated_usd=amount, reasoning=reason,
                ))
        return sorted(allocations, key=lambda a: a.capital_efficiency, reverse=True)

    def equal_split(self, total_capital_usd: float) -> dict[str, float]:
        """Naive baseline for comparison."""
        businesses = self.registry.list()
        if not businesses:
            return {}
        share = total_capital_usd / len(businesses)
        return {b.id: share for b in businesses}
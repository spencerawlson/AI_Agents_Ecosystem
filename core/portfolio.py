"""Cross-business analytics: benchmarks and shared learnings."""

from __future__ import annotations

from core.allocation import CapitalAllocator
from core.ledger import Ledger
from core.registry import BusinessRegistry


class PortfolioAnalytics:
    def __init__(
        self,
        registry: BusinessRegistry | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.registry = registry or BusinessRegistry()
        self.ledger = ledger or Ledger()

    def benchmarks(self) -> dict:
        """Per-metric best/worst/median across businesses."""
        businesses = self.registry.list()
        if not businesses:
            return {}
        metrics: dict[str, list[float]] = {
            "net_margin": [], "gross_margin": [], "capital_efficiency": [],
        }
        for biz in businesses:
            pnl = self.ledger.pnl(biz.id)
            metrics["net_margin"].append(pnl.net_margin)
            metrics["gross_margin"].append(pnl.gross_margin)
            metrics["capital_efficiency"].append(self.ledger.capital_efficiency(biz.id))
        out: dict = {}
        for name, values in metrics.items():
            s = sorted(values)
            out[name] = {
                "best": s[-1], "worst": s[0],
                "median": s[len(s) // 2],
            }
        return out

    def rankings(self) -> list[dict]:
        """Businesses ranked by net profit."""
        rows = []
        for biz in self.registry.list():
            pnl = self.ledger.pnl(biz.id)
            rows.append({
                "business_id": biz.id,
                "name": biz.name,
                "revenue": pnl.revenue,
                "net_profit": pnl.net_profit,
                "net_margin": pnl.net_margin,
            })
        return sorted(rows, key=lambda r: r["net_profit"], reverse=True)

    def learnings(self) -> list[dict]:
        """Shared learnings: top performers' traits worth replicating."""
        ranked = self.rankings()
        if len(ranked) < 2:
            return []
        top, bottom = ranked[0], ranked[-1]
        learnings = []
        if top["net_margin"] > bottom["net_margin"] + 0.1:
            learnings.append({
                "pattern": "margin_advantage",
                "detail": f"{top['name']} ({top['net_margin']:.0%} margin) vs "
                          f"{bottom['name']} ({bottom['net_margin']:.0%}) — "
                          "study top performer's pricing/cost structure",
            })
        return learnings
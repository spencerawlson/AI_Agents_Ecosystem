"""Portfolio experiments: run parallel experiments with a portfolio-level cap.

Each experiment has its own budget, and the portfolio enforces a
global ceiling so parallel experiments can't collectively blow
the budget.
"""

from __future__ import annotations

from core.experiments import Experiment, ExperimentActuals, ExperimentEngine
from core.models import ExperimentStatus


class PortfolioBudgetExceeded(RuntimeError):
    pass


class PortfolioExperiments:
    def __init__(
        self,
        engine: ExperimentEngine | None = None,
        portfolio_budget_usd: float = 5000.0,
    ) -> None:
        self.engine = engine or ExperimentEngine()
        self.portfolio_budget_usd = portfolio_budget_usd

    @property
    def committed(self) -> float:
        """Total capital committed across running experiments."""
        return sum(
            e.initial_capital_usd for e in self.engine._experiments.values()
            if e.status == ExperimentStatus.RUNNING
        )

    def launch(self, business_id: str, hypothesis: str, capital_usd: float,
               duration_days: int = 30) -> Experiment:
        if self.committed + capital_usd > self.portfolio_budget_usd:
            raise PortfolioBudgetExceeded(
                f"launching ${capital_usd:.2f} would exceed portfolio cap "
                f"${self.portfolio_budget_usd:.2f} "
                f"(${self.committed:.2f} committed)"
            )
        return self.engine.start(
            business_id=business_id,
            initial_capital_usd=capital_usd,
            max_advertising_usd=capital_usd * 0.3,
            duration_days=duration_days,
        )

    def evaluate_all(self) -> dict[str, str]:
        """Evaluate every running experiment; return {experiment_id: recommendation}."""
        results: dict[str, str] = {}
        for exp in self.engine._experiments.values():
            if exp.status == ExperimentStatus.RUNNING:
                # Evaluate with zero actuals (no data yet) — engine handles it.
                results[exp.id] = self.engine.evaluate(exp.id, ExperimentActuals())
        return results

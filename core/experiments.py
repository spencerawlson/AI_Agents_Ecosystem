"""Experiment engine: bounded business experiments with evaluation.

An experiment has fixed capital, a fixed ad budget, a duration, and
measurable targets. Evaluation compares actuals against targets and
recommends SCALE / MAINTAIN / OPTIMIZE / PAUSE / SHUT DOWN.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.models import utcnow
from .models import Experiment, ExperimentStatus


@dataclass
class ExperimentActuals:
    revenue_usd: float = 0.0
    operating_cost_usd: float = 0.0
    advertising_usd: float = 0.0
    orders: int = 0
    visitors: int = 0
    refunds: int = 0
    customers_acquired: int = 0


class ExperimentEngine:
    def __init__(self) -> None:
        self._experiments: dict[str, Experiment] = {}

    def start(
        self,
        business_id: str,
        initial_capital_usd: float,
        max_advertising_usd: float,
        duration_days: int,
        **targets,
    ) -> Experiment:
        exp = Experiment(
            business_id=business_id,
            initial_capital_usd=initial_capital_usd,
            max_advertising_usd=max_advertising_usd,
            duration_days=duration_days,
            status=ExperimentStatus.RUNNING,
            started_at=utcnow(),
            **targets,
        )
        self._experiments[exp.id] = exp
        return exp

    def get(self, experiment_id: str) -> Experiment | None:
        return self._experiments.get(experiment_id)

    def evaluate(self, experiment_id: str, actuals: ExperimentActuals) -> str:
        """Compare actuals to targets; return and record the recommendation."""
        exp = self.get(experiment_id)
        if exp is None:
            raise ValueError(f"unknown experiment: {experiment_id}")

        profit = actuals.revenue_usd - actuals.operating_cost_usd
        roas = (
            actuals.revenue_usd / actuals.advertising_usd
            if actuals.advertising_usd > 0
            else float("inf")
        )
        conversion = actuals.orders / actuals.visitors if actuals.visitors > 0 else 0.0
        refund_rate = actuals.refunds / actuals.orders if actuals.orders > 0 else 0.0
        cac = (
            actuals.advertising_usd / actuals.customers_acquired
            if actuals.customers_acquired > 0
            else float("inf")
        )
        gross_margin = profit / actuals.revenue_usd if actuals.revenue_usd > 0 else 0.0

        checks: list[bool] = []
        if exp.target_roas_min is not None:
            checks.append(roas >= exp.target_roas_min)
        if exp.target_conversion_min is not None:
            checks.append(conversion >= exp.target_conversion_min)
        if exp.target_cac_max is not None:
            checks.append(cac <= exp.target_cac_max)
        if exp.target_gross_margin_min is not None:
            checks.append(gross_margin >= exp.target_gross_margin_min)
        if exp.max_refund_rate is not None:
            checks.append(refund_rate <= exp.max_refund_rate)
        # Profitability is always required.
        checks.append(profit > 0)

        passed = sum(checks)
        total = len(checks)

        if passed == total:
            recommendation = "SCALE"
        elif passed >= total * 0.66:
            recommendation = "OPTIMIZE"
        elif profit > 0:
            recommendation = "MAINTAIN"
        elif passed == 0:
            recommendation = "SHUT DOWN"
        else:
            recommendation = "PAUSE"

        exp.recommendation = recommendation
        exp.status = ExperimentStatus.COMPLETED
        exp.ended_at = utcnow()
        return recommendation

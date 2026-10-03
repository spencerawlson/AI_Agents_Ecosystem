"""Anomaly detection and AI model routing / cost optimization.

Anomaly detection: flag metric deviations beyond N standard deviations
from the rolling baseline.

Model routing: pick the cheapest model capable of a task, so the
ecosystem minimizes its own AI inference spend.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field


class AnomalyDetector:
    def __init__(self, window: int = 30, threshold_std: float = 2.0) -> None:
        self.window = window
        self.threshold_std = threshold_std
        self._series: dict[str, list[float]] = {}

    def observe(self, metric: str, value: float) -> bool:
        """Record a value; return True if it's anomalous."""
        history = self._series.setdefault(metric, [])
        is_anomaly = False
        if len(history) >= 5:
            mean = statistics.mean(history[-self.window:])
            stdev = statistics.stdev(history[-self.window:]) if len(history) > 1 else 0
            if stdev > 0:
                is_anomaly = abs(value - mean) > self.threshold_std * stdev
            elif mean != 0:
                # Flat baseline: any deviation >50% is anomalous.
                is_anomaly = abs(value - mean) / abs(mean) > 0.5
        history.append(value)
        return is_anomaly

    def baseline(self, metric: str) -> tuple[float, float] | None:
        """(mean, stdev) of recent history, or None if insufficient data."""
        history = self._series.get(metric, [])
        if len(history) < 5:
            return None
        recent = history[-self.window:]
        return (statistics.mean(recent),
                statistics.stdev(recent) if len(recent) > 1 else 0.0)


@dataclass
class ModelOption:
    name: str
    cost_per_1k_tokens: float
    capability_tier: int  # 1 = simple, 2 = standard, 3 = complex


class ModelRouter:
    """Route each task to the cheapest model that can handle it."""

    def __init__(self, models: list[ModelOption] | None = None) -> None:
        self.models = models or [
            ModelOption("fast", cost_per_1k_tokens=0.001, capability_tier=1),
            ModelOption("balanced", cost_per_1k_tokens=0.01, capability_tier=2),
            ModelOption("powerful", cost_per_1k_tokens=0.05, capability_tier=3),
        ]
        self._spend_log: list[tuple[str, str, float, int]] = []  # (task, model, cost, tokens)

    def route(self, task_description: str, required_tier: int) -> ModelOption:
        """Cheapest model at or above the required capability tier."""
        candidates = [m for m in self.models if m.capability_tier >= required_tier]
        if not candidates:
            raise ValueError(f"no model meets tier {required_tier}")
        return min(candidates, key=lambda m: m.cost_per_1k_tokens)

    def log_spend(self, task_description: str, model: ModelOption, tokens: int) -> float:
        cost = tokens / 1000 * model.cost_per_1k_tokens
        self._spend_log.append((task_description, model.name, cost, tokens))
        return cost

    def total_ai_spend(self) -> float:
        return sum(cost for _, _, cost, _ in self._spend_log)

    def savings_vs_always_powerful(self) -> float:
        """Dollars saved vs routing every logged task to the top tier."""
        powerful = max(self.models, key=lambda m: m.capability_tier)
        saved = 0.0
        for _, model_name, actual_cost, tokens in self._spend_log:
            if model_name != powerful.name:
                powerful_cost = tokens / 1000 * powerful.cost_per_1k_tokens
                saved += powerful_cost - actual_cost
        return saved
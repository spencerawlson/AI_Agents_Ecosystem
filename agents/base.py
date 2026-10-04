"""Base agent: typed tasks, budget enforcement, structured outputs.

Subclasses implement `run()` and return a dict. The base class tracks
token usage and cost against the task budget and raises BudgetExceeded
before the agent can overspend.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any

from orchestrator.models import Task


class BudgetExceeded(RuntimeError):
    pass


class BaseAgent(ABC):
    """Base class for all ecosystem agents."""

    agent_type: str = "base"
    capabilities: list[str] = []
    description: str = ""

    def __init__(self) -> None:
        # Usage counters are thread-local: one handler instance may serve
        # concurrent tasks on the dispatcher, and per-run token/cost deltas
        # must stay exact (no lost updates, no cross-talk between runs).
        self._local = threading.local()
        self._reset_usage()

    @abstractmethod
    def run(self, task: Task) -> dict:
        """Execute the task. Must return a JSON-serializable dict."""
        ...

    def self_check(self, task: Task, output: dict) -> list[str]:
        """Layer 1 of verification: the agent checks its own output.

        Return a list of issue strings; empty means the agent stands behind
        its output. Override to enforce the task's acceptance criteria from
        the producer's side — a real quality bar, not a rubber stamp.
        """
        return []

    def _reset_usage(self) -> None:
        """Reset the *current thread's* usage counters (called per task)."""
        self._local.tokens_used = 0
        self._local.cost_usd = 0.0
        self._local.last_self_check = []

    def __call__(self, task: Task) -> dict:
        self._reset_usage()
        output = self.run(task)
        if not isinstance(output, dict):
            raise TypeError(f"{self.agent_type} must return a dict")
        try:
            self._last_self_check = list(self.self_check(task, output) or [])
        except Exception as exc:  # noqa: BLE001 - a crashing self-check is itself a finding
            self._last_self_check = [f"self_check crashed: {exc}"]
        return output

    # -- budget-tracked resource usage -----------------------------------

    def record_usage(self, task: Task, tokens: int, cost_usd: float) -> None:
        """Record LLM/API usage. Raises BudgetExceeded if over budget."""
        tokens_used = getattr(self._local, "tokens_used", 0) + tokens
        cost_used = getattr(self._local, "cost_usd", 0.0) + cost_usd
        self._local.tokens_used = tokens_used
        self._local.cost_usd = cost_used
        if task.budget_tokens > 0 and tokens_used > task.budget_tokens:
            raise BudgetExceeded(
                f"{self.agent_type}: token budget exceeded "
                f"({tokens_used} > {task.budget_tokens})"
            )
        if task.budget_usd > 0 and cost_used > task.budget_usd:
            raise BudgetExceeded(
                f"{self.agent_type}: dollar budget exceeded "
                f"(${cost_used:.2f} > ${task.budget_usd:.2f})"
            )

    @property
    def tokens_used(self) -> int:
        return getattr(self._local, "tokens_used", 0)

    @property
    def cost_usd(self) -> float:
        return getattr(self._local, "cost_usd", 0.0)

    @property
    def _last_self_check(self) -> list[str]:
        # Written by __call__, read by the orchestrator on the same thread.
        return getattr(self._local, "last_self_check", [])

    @_last_self_check.setter
    def _last_self_check(self, value: list[str]) -> None:
        self._local.last_self_check = list(value)

    def validate_output(self, output: Any, schema: type) -> Any:
        """Validate structured output against a Pydantic schema."""
        return schema.model_validate(output)

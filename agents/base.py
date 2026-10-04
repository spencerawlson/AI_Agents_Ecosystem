"""Base agent: typed tasks, budget enforcement, structured outputs.

Subclasses implement `run()` and return a dict. The base class tracks
token usage and cost against the task budget and raises BudgetExceeded
before the agent can overspend.
"""

from __future__ import annotations

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
        self._tokens_used = 0
        self._cost_usd = 0.0
        self._last_self_check: list[str] = []

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

    def __call__(self, task: Task) -> dict:
        self._tokens_used = 0
        self._cost_usd = 0.0
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
        self._tokens_used += tokens
        self._cost_usd += cost_usd
        if task.budget_tokens > 0 and self._tokens_used > task.budget_tokens:
            raise BudgetExceeded(
                f"{self.agent_type}: token budget exceeded "
                f"({self._tokens_used} > {task.budget_tokens})"
            )
        if task.budget_usd > 0 and self._cost_usd > task.budget_usd:
            raise BudgetExceeded(
                f"{self.agent_type}: dollar budget exceeded "
                f"(${self._cost_usd:.2f} > ${task.budget_usd:.2f})"
            )

    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    @property
    def cost_usd(self) -> float:
        return self._cost_usd

    def validate_output(self, output: Any, schema: type) -> Any:
        """Validate structured output against a Pydantic schema."""
        return schema.model_validate(output)

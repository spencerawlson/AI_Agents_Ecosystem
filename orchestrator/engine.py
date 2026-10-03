"""Orchestrator engine: schedules tasks, dispatches to agents, tracks runs.

The store interface is deliberately narrow so a PostgreSQL backend can
replace the in-memory implementation without touching dispatch logic.
"""

from __future__ import annotations

from typing import Protocol

from core.logging import get_logger
from .models import AgentRun, Approval, Task, TaskStatus, utcnow
from .registry import AgentRegistry

log = get_logger("orchestrator")


class TaskStore(Protocol):
    def save_task(self, task: Task) -> None: ...
    def get_task(self, task_id: str) -> Task | None: ...
    def list_tasks(self, status: TaskStatus | None = None) -> list[Task]: ...
    def save_run(self, run: AgentRun) -> None: ...
    def get_run(self, run_id: str) -> AgentRun | None: ...


class MemoryTaskStore:
    """In-memory store. Replace with Postgres for production."""

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.runs: dict[str, AgentRun] = {}

    def save_task(self, task: Task) -> None:
        self.tasks[task.id] = task

    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        tasks = list(self.tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        return sorted(tasks, key=lambda t: t.created_at)

    def save_run(self, run: AgentRun) -> None:
        self.runs[run.id] = run

    def get_run(self, run_id: str) -> AgentRun | None:
        return self.runs.get(run_id)


class Orchestrator:
    """Central coordination: schedule tasks, dispatch to agents, track runs."""

    def __init__(
        self,
        registry: AgentRegistry | None = None,
        store: TaskStore | None = None,
    ) -> None:
        self.registry = registry or AgentRegistry()
        self.store = store or MemoryTaskStore()
        self._handlers: dict[str, object] = {}

    def register_handler(self, agent_type: str, handler: object) -> None:
        """Register the callable/instance that executes tasks for an agent type."""
        self._handlers[agent_type] = handler

    def submit(
        self,
        agent_type: str,
        inputs: dict | None = None,
        business_id: str | None = None,
        budget_usd: float = 0.0,
        budget_tokens: int = 0,
        deadline=None,
    ) -> Task:
        if self.registry.get(agent_type) is None:
            raise ValueError(f"unknown agent type: {agent_type}")
        if budget_usd < 0 or budget_tokens < 0:
            raise ValueError("budgets cannot be negative")
        task = Task(
            agent_type=agent_type,
            business_id=business_id,
            inputs=inputs or {},
            budget_usd=budget_usd,
            budget_tokens=budget_tokens,
            deadline=deadline,
        )
        self.store.save_task(task)
        log.info("task_submitted id=%s agent=%s business=%s", task.id, agent_type, business_id)
        return task

    def dispatch(self, task_id: str) -> AgentRun:
        """Execute a pending task synchronously and record the run."""
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"unknown task: {task_id}")
        if task.status != TaskStatus.PENDING:
            raise ValueError(f"task {task_id} is not pending (status={task.status})")
        handler = self._handlers.get(task.agent_type)
        if handler is None:
            raise ValueError(f"no handler registered for agent type: {task.agent_type}")

        task.status = TaskStatus.RUNNING
        task.started_at = utcnow()
        self.store.save_task(task)

        run = AgentRun(task_id=task.id, agent_type=task.agent_type, business_id=task.business_id)
        self.store.save_run(run)

        try:
            output = handler(task)  # type: ignore[operator]
            if not isinstance(output, dict):
                raise TypeError(f"agent {task.agent_type} must return a dict, got {type(output)}")
            run.output = output
            # Capture budget-tracked usage from BaseAgent handlers.
            tokens = getattr(handler, "tokens_used", 0)
            cost = getattr(handler, "cost_usd", 0.0)
            run.tokens_used = int(tokens) if isinstance(tokens, (int, float)) else 0
            run.cost_usd = float(cost) if isinstance(cost, (int, float)) else 0.0
            run.status = TaskStatus.COMPLETED
            task.status = TaskStatus.COMPLETED
            log.info(
                "task_completed id=%s run=%s tokens=%d cost_usd=%.4f",
                task.id, run.id, run.tokens_used, run.cost_usd,
            )
        except Exception as exc:  # noqa: BLE001
            run.status = TaskStatus.FAILED
            run.error = str(exc)
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            log.warning("task_failed id=%s run=%s error=%s", task.id, run.id, exc)
        finally:
            run.finished_at = utcnow()
            task.finished_at = run.finished_at
            self.store.save_run(run)
            self.store.save_task(task)

        return run

    def pending_tasks(self) -> list[Task]:
        return self.store.list_tasks(status=TaskStatus.PENDING)

    def run_next(self) -> AgentRun | None:
        """Dispatch the oldest pending task, if any."""
        pending = self.pending_tasks()
        if not pending:
            return None
        return self.dispatch(pending[0].id)
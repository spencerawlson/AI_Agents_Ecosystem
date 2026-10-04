"""Orchestrator engine: schedules tasks, dispatches to agents, tracks runs.

The store interface is deliberately narrow so a PostgreSQL backend can
replace the in-memory implementation without touching dispatch logic.
"""

from __future__ import annotations

from typing import Protocol

from core.logging import get_logger
from core.quality import rework_feedback, verify_output
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
        audit=None,
    ) -> None:
        self.registry = registry or AgentRegistry()
        self.store = store or MemoryTaskStore()
        self._handlers: dict[str, object] = {}
        self._audit = audit

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
        acceptance_criteria: list[str] | None = None,
        max_rework: int = 2,
    ) -> Task:
        if self.registry.get(agent_type) is None:
            raise ValueError(f"unknown agent type: {agent_type}")
        if budget_usd < 0 or budget_tokens < 0:
            raise ValueError("budgets cannot be negative")
        if max_rework < 0:
            raise ValueError("max_rework cannot be negative")
        task = Task(
            agent_type=agent_type,
            business_id=business_id,
            inputs=inputs or {},
            budget_usd=budget_usd,
            budget_tokens=budget_tokens,
            deadline=deadline,
            acceptance_criteria=acceptance_criteria or [],
            max_rework=max_rework,
        )
        self.store.save_task(task)
        log.info("task_submitted id=%s agent=%s business=%s", task.id, agent_type, business_id)
        return task

    def _peer_reviewer(self):
        """Return the registered review agent's review() callable, if any."""
        reviewer = self._handlers.get("review")
        fn = getattr(reviewer, "review", None)
        return fn if callable(fn) else None

    def _audit_event(self, **kwargs) -> None:
        if self._audit is not None:
            try:
                self._audit.record(**kwargs)
            except Exception:  # noqa: BLE001 - audit must never break dispatch
                log.warning("audit record failed", exc_info=True)

    def dispatch(self, task_id: str) -> AgentRun:
        """Execute a pending task, verify its output, and record the run.

        The output must clear all five verification layers (quintuple
        check). Failures are fed back to the agent as rework instructions,
        bounded by Task.max_rework. The returned run is the final attempt.
        """
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

        run = AgentRun(task_id=task.id, agent_type=task.agent_type,
                       business_id=task.business_id)
        while True:
            run.rework_count = task.rework_count
            self.store.save_run(run)
            try:
                output = handler(task)  # type: ignore[operator]
                if not isinstance(output, dict):
                    raise TypeError(
                        f"agent {task.agent_type} must return a dict, got {type(output)}")
                run.output = output
                # Capture budget-tracked usage from BaseAgent handlers.
                tokens = getattr(handler, "tokens_used", 0)
                cost = getattr(handler, "cost_usd", 0.0)
                run.tokens_used = int(tokens) if isinstance(tokens, (int, float)) else 0
                run.cost_usd = float(cost) if isinstance(cost, (int, float)) else 0.0

                report = verify_output(
                    task,
                    output,
                    self_check_issues=getattr(handler, "_last_self_check", []),
                    peer_reviewer=self._peer_reviewer(),
                )
                run.verification = report.to_dict()

                if report.passed:
                    run.status = TaskStatus.COMPLETED
                    task.status = TaskStatus.COMPLETED
                    self._audit_event(
                        agent_type=task.agent_type, task_id=task.id, run_id=run.id,
                        business_id=task.business_id, event="task_verified",
                        result={"passed": True, "rework_count": task.rework_count},
                        tokens_used=run.tokens_used, cost_usd=run.cost_usd,
                    )
                    log.info(
                        "task_completed id=%s run=%s tokens=%d cost_usd=%.4f rework=%d",
                        task.id, run.id, run.tokens_used, run.cost_usd, task.rework_count,
                    )
                    break

                # Verification failed: rework or give up.
                if task.rework_count >= task.max_rework:
                    run.status = TaskStatus.FAILED
                    run.error = "verification failed: " + "; ".join(report.failures)
                    task.status = TaskStatus.FAILED
                    task.error = run.error
                    self._audit_event(
                        agent_type=task.agent_type, task_id=task.id, run_id=run.id,
                        business_id=task.business_id, event="task_verified",
                        result={"passed": False, "failures": report.failures},
                        error=run.error,
                    )
                    log.warning("task_failed id=%s run=%s error=%s",
                                task.id, run.id, run.error)
                    break

                task.rework_count += 1
                task.inputs["_rework_feedback"] = rework_feedback(report)
                task.inputs["_rework_attempt"] = task.rework_count
                run.status = TaskStatus.FAILED
                run.error = (f"verification failed; rework requested "
                             f"(attempt {task.rework_count}): "
                             + "; ".join(report.failures))
                self.store.save_run(run)
                self.store.save_task(task)
                self._audit_event(
                    agent_type=task.agent_type, task_id=task.id, run_id=run.id,
                    business_id=task.business_id, event="task_rework_requested",
                    result={"attempt": task.rework_count,
                            "failures": report.failures},
                )
                log.info("task_rework id=%s attempt=%d failures=%d",
                         task.id, task.rework_count, len(report.failures))
                run = AgentRun(task_id=task.id, agent_type=task.agent_type,
                               business_id=task.business_id)
            except Exception as exc:  # noqa: BLE001
                run.status = TaskStatus.FAILED
                run.error = str(exc)
                task.status = TaskStatus.FAILED
                task.error = str(exc)
                log.warning("task_failed id=%s run=%s error=%s", task.id, run.id, exc)
                break

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

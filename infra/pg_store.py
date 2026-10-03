"""PostgreSQL-backed task store for the orchestrator.

Implements the TaskStore protocol using SQLAlchemy. Drop-in replacement
for MemoryTaskStore.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from infra.db_models import AgentRunRow, TaskRow
from orchestrator.models import AgentRun, Task, TaskStatus


def _row_to_task(row: TaskRow) -> Task:
    return Task(
        id=row.id,
        agent_type=row.agent_type,
        business_id=row.business_id,
        inputs=row.inputs or {},
        budget_usd=row.budget_usd,
        budget_tokens=row.budget_tokens,
        status=TaskStatus(row.status),
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
    )


def _row_to_run(row: AgentRunRow) -> AgentRun:
    return AgentRun(
        id=row.id,
        task_id=row.task_id,
        agent_type=row.agent_type,
        business_id=row.business_id,
        status=TaskStatus(row.status),
        output=row.output,
        tokens_used=row.tokens_used,
        cost_usd=row.cost_usd,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
    )


class PostgresTaskStore:
    """TaskStore backed by PostgreSQL."""

    def __init__(self, database_url: str | None = None) -> None:
        url = (database_url or os.getenv(
            "DATABASE_URL", "postgresql://ecosystem:ecosystem@localhost:5432/ecosystem"
        )).replace("+asyncpg", "")
        self.engine = create_engine(url)
        self._session: sessionmaker[Session] = sessionmaker(bind=self.engine)

    def save_task(self, task: Task) -> None:
        with self._session() as s:
            row = s.get(TaskRow, task.id)
            if row is None:
                row = TaskRow(id=task.id)
                s.add(row)
            row.agent_type = task.agent_type
            row.business_id = task.business_id
            row.inputs = task.inputs
            row.budget_usd = task.budget_usd
            row.budget_tokens = task.budget_tokens
            row.status = task.status.value
            row.error = task.error
            row.started_at = task.started_at
            row.finished_at = task.finished_at
            s.commit()

    def get_task(self, task_id: str) -> Task | None:
        with self._session() as s:
            row = s.get(TaskRow, task_id)
            return _row_to_task(row) if row else None

    def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        with self._session() as s:
            q = s.query(TaskRow).order_by(TaskRow.created_at)
            if status is not None:
                q = q.filter(TaskRow.status == status.value)
            return [_row_to_task(r) for r in q.all()]

    def save_run(self, run: AgentRun) -> None:
        with self._session() as s:
            row = s.get(AgentRunRow, run.id)
            if row is None:
                row = AgentRunRow(id=run.id)
                s.add(row)
            row.task_id = run.task_id
            row.agent_type = run.agent_type
            row.business_id = run.business_id
            row.status = run.status.value
            row.output = run.output
            row.tokens_used = run.tokens_used
            row.cost_usd = run.cost_usd
            row.error = run.error
            row.started_at = run.started_at
            row.finished_at = run.finished_at
            s.commit()

    def get_run(self, run_id: str) -> AgentRun | None:
        with self._session() as s:
            row = s.get(AgentRunRow, run_id)
            return _row_to_run(row) if row else None
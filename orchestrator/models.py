"""Core domain models for the orchestrator: tasks, runs, approvals."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLED = "cancelled"


class Task(BaseModel):
    """A unit of work assigned to an agent."""

    id: str = Field(default_factory=lambda: new_id("task"))
    agent_type: str
    business_id: str | None = None
    inputs: dict = Field(default_factory=dict)
    budget_usd: float = 0.0
    budget_tokens: int = 0
    deadline: datetime | None = None
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    # -- quality: five-layer verification ("quintuple check") -------------
    acceptance_criteria: list[str] = Field(default_factory=list)
    max_rework: int = 2
    rework_count: int = 0


class AgentRun(BaseModel):
    """A single execution of a task by an agent."""

    id: str = Field(default_factory=lambda: new_id("run"))
    task_id: str
    agent_type: str
    business_id: str | None = None
    status: TaskStatus = TaskStatus.RUNNING
    output: dict | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    error: str | None = None
    # -- quality -----------------------------------------------------------
    verification: dict | None = None
    rework_count: int = 0


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Approval(BaseModel):
    """A human approval gate for a consequential action."""

    id: str = Field(default_factory=lambda: new_id("appr"))
    task_id: str | None = None
    business_id: str | None = None
    action: str
    details: dict = Field(default_factory=dict)
    amount_usd: float | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: str = "orchestrator"
    requested_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str | None = None


class AuditEvent(BaseModel):
    """An immutable record of an agent action."""

    id: str = Field(default_factory=lambda: new_id("evt"))
    agent_type: str
    task_id: str | None = None
    run_id: str | None = None
    business_id: str | None = None
    event: str
    inputs: dict = Field(default_factory=dict)
    data_sources: list[str] = Field(default_factory=list)
    decision: str | None = None
    action: str | None = None
    result: dict | None = None
    error: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    approval_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

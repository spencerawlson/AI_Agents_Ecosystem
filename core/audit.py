"""Audit log: immutable record of every consequential agent action.

Answers: what did the AI do, why, with what data, at what cost,
and did it generate measurable value.
"""

from __future__ import annotations

from orchestrator.models import AuditEvent


class AuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(
        self,
        agent_type: str,
        event: str,
        task_id: str | None = None,
        run_id: str | None = None,
        business_id: str | None = None,
        inputs: dict | None = None,
        data_sources: list[str] | None = None,
        decision: str | None = None,
        action: str | None = None,
        result: dict | None = None,
        error: str | None = None,
        tokens_used: int = 0,
        cost_usd: float = 0.0,
        approval_id: str | None = None,
    ) -> AuditEvent:
        evt = AuditEvent(
            agent_type=agent_type,
            task_id=task_id,
            run_id=run_id,
            business_id=business_id,
            event=event,
            inputs=inputs or {},
            data_sources=data_sources or [],
            decision=decision,
            action=action,
            result=result,
            error=error,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            approval_id=approval_id,
        )
        self._events.append(evt)
        return evt

    def for_business(self, business_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.business_id == business_id]

    def for_task(self, task_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.task_id == task_id]

    def total_ai_cost(self, business_id: str | None = None) -> float:
        events = self._events if business_id is None else self.for_business(business_id)
        return sum(e.cost_usd for e in events)

    def __len__(self) -> int:
        return len(self._events)
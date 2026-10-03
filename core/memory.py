"""Agent memory: persistent record of what the ecosystem has learned.

Stores: experiment outcomes, failures, campaign performance,
supplier track records, pricing history. Agents query memory
to avoid repeating mistakes and replicate successes.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from orchestrator.models import new_id, utcnow


class MemoryType(str, Enum):
    EXPERIMENT = "experiment"
    FAILURE = "failure"
    CAMPAIGN = "campaign"
    SUPPLIER = "supplier"
    PRICING = "pricing"
    INSIGHT = "insight"


class Memory(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    memory_type: MemoryType
    business_id: str | None = None
    title: str
    detail: str = ""
    # Structured outcome for learning: -1 (bad) to +1 (good).
    outcome_score: float = 0.0
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class AgentMemory:
    def __init__(self) -> None:
        self._memories: list[Memory] = []

    def remember(
        self,
        memory_type: MemoryType,
        title: str,
        detail: str = "",
        business_id: str | None = None,
        outcome_score: float = 0.0,
        tags: list[str] | None = None,
    ) -> Memory:
        mem = Memory(
            memory_type=memory_type,
            business_id=business_id,
            title=title,
            detail=detail,
            outcome_score=outcome_score,
            tags=tags or [],
        )
        self._memories.append(mem)
        return mem

    def recall(
        self,
        memory_type: MemoryType | None = None,
        business_id: str | None = None,
        tag: str | None = None,
        min_outcome: float | None = None,
    ) -> list[Memory]:
        results = self._memories
        if memory_type:
            results = [m for m in results if m.memory_type == memory_type]
        if business_id:
            results = [m for m in results if m.business_id == business_id]
        if tag:
            results = [m for m in results if tag in m.tags]
        if min_outcome is not None:
            results = [m for m in results if m.outcome_score >= min_outcome]
        return results

    def failures(self, business_id: str | None = None) -> list[Memory]:
        """What went wrong — checked before repeating an approach."""
        return self.recall(MemoryType.FAILURE, business_id=business_id)

    def successes(self, business_id: str | None = None) -> list[Memory]:
        """What worked — candidates for replication."""
        return self.recall(business_id=business_id, min_outcome=0.5)

    def supplier_rating(self, supplier_name: str) -> float | None:
        """Average outcome score for a supplier, or None if unknown."""
        records = [m for m in self._memories
                   if m.memory_type == MemoryType.SUPPLIER
                   and supplier_name.lower() in m.title.lower()]
        if not records:
            return None
        return sum(m.outcome_score for m in records) / len(records)
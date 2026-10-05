"""Analytics: traffic, conversion funnels, cohorts.

Event-sourced: record raw events, derive funnels and cohorts.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from orchestrator.models import new_id, utcnow


class Event(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evt"))
    business_id: str
    event_type: str  # pageview | add_to_cart | checkout_start | purchase | refund
    session_id: str | None = None
    customer_id: str | None = None
    value_usd: float = 0.0
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class Analytics:
    def __init__(self) -> None:
        self._events: list[Event] = []

    def track(
        self,
        business_id: str,
        event_type: str,
        session_id: str | None = None,
        customer_id: str | None = None,
        value_usd: float = 0.0,
        **metadata,
    ) -> Event:
        evt = Event(
            business_id=business_id,
            event_type=event_type,
            session_id=session_id,
            customer_id=customer_id,
            value_usd=value_usd,
            metadata=metadata,
        )
        self._events.append(evt)
        return evt

    def funnel(self, business_id: str) -> dict[str, int]:
        """Count events per stage for a business."""
        stages = ["pageview", "add_to_cart", "checkout_start", "purchase"]
        counts: dict[str, int] = {}
        for stage in stages:
            counts[stage] = sum(
                1 for e in self._events
                if e.business_id == business_id and e.event_type == stage
            )
        return counts

    def conversion_rate(self, business_id: str) -> float:
        f = self.funnel(business_id)
        if f["pageview"] == 0:
            return 0.0
        return f["purchase"] / f["pageview"]

    def revenue(self, business_id: str) -> float:
        return sum(
            e.value_usd for e in self._events
            if e.business_id == business_id and e.event_type == "purchase"
        )

    def cohort_revenue(self, business_id: str) -> dict[str, float]:
        """Revenue grouped by customer cohort (first-purchase month)."""
        first_seen: dict[str, datetime] = {}
        for e in sorted(self._events, key=lambda e: e.created_at):
            if (e.business_id == business_id and e.customer_id
                    and e.event_type == "purchase"
                    and e.customer_id not in first_seen):
                first_seen[e.customer_id] = e.created_at
        cohorts: dict[str, float] = {}
        for e in self._events:
            if e.business_id == business_id and e.event_type == "purchase" and e.customer_id:
                cohort = first_seen[e.customer_id].strftime("%Y-%m")
                cohorts[cohort] = cohorts.get(cohort, 0.0) + e.value_usd
        return cohorts

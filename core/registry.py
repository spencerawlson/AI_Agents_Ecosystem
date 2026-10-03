"""Business registry: CRUD plus enforced state-machine transitions."""

from __future__ import annotations

from orchestrator.models import utcnow
from .models import TRANSITIONS, Business, BusinessStatus


class InvalidTransition(RuntimeError):
    pass


class BusinessRegistry:
    def __init__(self) -> None:
        self._businesses: dict[str, Business] = {}

    def create(self, name: str, business_type: str, **kwargs) -> Business:
        biz = Business(name=name, business_type=business_type, **kwargs)
        self._businesses[biz.id] = biz
        return biz

    def get(self, business_id: str) -> Business | None:
        return self._businesses.get(business_id)

    def list(self, status: BusinessStatus | None = None) -> list[Business]:
        businesses = list(self._businesses.values())
        if status is not None:
            businesses = [b for b in businesses if b.status == status]
        return sorted(businesses, key=lambda b: b.created_at)

    def transition(self, business_id: str, to: BusinessStatus) -> Business:
        biz = self.get(business_id)
        if biz is None:
            raise ValueError(f"unknown business: {business_id}")
        allowed = list(TRANSITIONS.get(biz.status, []))
        # Termination is always allowed except from already-terminal states.
        if to == BusinessStatus.TERMINATED and biz.status not in (
            BusinessStatus.MATURE,
            BusinessStatus.TERMINATED,
        ):
            allowed.append(BusinessStatus.TERMINATED)
        if to not in allowed:
            raise InvalidTransition(f"cannot transition {biz.status} -> {to}")
        biz.status = to
        biz.updated_at = utcnow()
        return biz

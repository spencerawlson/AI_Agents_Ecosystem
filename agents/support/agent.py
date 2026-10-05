"""Customer Service Agent: routine interactions with escalation rules.

Handles FAQs, order/shipping questions, product info, basic troubleshooting.
Escalates to humans: high-value refunds, chargebacks, legal threats,
fraud indicators, unusual complaints, sensitive situations.
"""

from __future__ import annotations

import threading
from enum import Enum

from pydantic import BaseModel, Field

from agents.base import BaseAgent
from orchestrator.models import Task


class TicketStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class Ticket(BaseModel):
    id: str
    business_id: str
    customer_id: str
    subject: str
    category: str  # faq | order | shipping | product | refund | complaint | other
    status: TicketStatus = TicketStatus.OPEN
    resolution: str | None = None
    escalated_reason: str | None = None


# Categories/phrases that always escalate.
ESCALATION_TRIGGERS = {
    "chargeback",
    "legal",
    "lawyer",
    "fraud",
    "lawsuit",
    "attorney",
}

# Refund amounts above this always escalate.
REFUND_ESCALATION_THRESHOLD_USD = 100.0


class SupportAgent(BaseAgent):
    agent_type = "support"
    capabilities = [
        "faq",
        "order_support",
        "refund_triage",
        "complaint_classification",
    ]
    description = "Customer service with human escalation rules."

    def __init__(self) -> None:
        super().__init__()
        self._tickets: dict[str, Ticket] = {}
        self._counter = 0
        # Guarded for concurrent dispatcher tasks on one handler instance.
        self._id_lock = threading.Lock()

    def _next_id(self) -> str:
        with self._id_lock:
            self._counter += 1
            return f"tkt_{self._counter:05d}"

    def _needs_escalation(self, subject: str, category: str, amount_usd: float | None) -> str | None:
        lowered = subject.lower()
        for trigger in ESCALATION_TRIGGERS:
            if trigger in lowered:
                return f"trigger phrase: {trigger}"
        if category == "refund" and amount_usd is not None and amount_usd > REFUND_ESCALATION_THRESHOLD_USD:
            return f"refund ${amount_usd:.2f} exceeds ${REFUND_ESCALATION_THRESHOLD_USD:.2f}"
        return None

    def open_ticket(
        self,
        business_id: str,
        customer_id: str,
        subject: str,
        category: str = "other",
        amount_usd: float | None = None,
    ) -> Ticket:
        reason = self._needs_escalation(subject, category, amount_usd)
        ticket = Ticket(
            id=self._next_id(),
            business_id=business_id,
            customer_id=customer_id,
            subject=subject,
            category=category,
            status=TicketStatus.ESCALATED if reason else TicketStatus.OPEN,
            escalated_reason=reason,
        )
        self._tickets[ticket.id] = ticket
        return ticket

    def resolve(self, ticket_id: str, resolution: str) -> Ticket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise ValueError(f"unknown ticket: {ticket_id}")
        if ticket.status == TicketStatus.ESCALATED:
            raise ValueError(f"ticket {ticket_id} is escalated; human must resolve")
        ticket.status = TicketStatus.RESOLVED
        ticket.resolution = resolution
        return ticket

    def escalated(self) -> list[Ticket]:
        return [t for t in self._tickets.values() if t.status == TicketStatus.ESCALATED]

    def run(self, task: Task) -> dict:
        action = task.inputs.get("action", "report")
        if action == "report":
            tickets = list(self._tickets.values())
            self.record_usage(task, tokens=200, cost_usd=0.01)
            return {
                "total": len(tickets),
                "open": sum(1 for t in tickets if t.status == TicketStatus.OPEN),
                "resolved": sum(1 for t in tickets if t.status == TicketStatus.RESOLVED),
                "escalated": sum(1 for t in tickets if t.status == TicketStatus.ESCALATED),
            }
        raise ValueError(f"unknown action: {action}")

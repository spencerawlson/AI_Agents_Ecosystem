"""Operations Agent: monitors business health, raises incidents.

Watches: orders, inventory, fulfillment, website availability,
payment failures, support backlog. Incidents generate structured
alerts with severity; critical ones can trigger approval-gated
remediation workflows.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from agents.base import BaseAgent
from orchestrator.models import Task, utcnow


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Incident(BaseModel):
    id: str
    business_id: str
    category: str  # order | inventory | fulfillment | uptime | payment | support
    severity: Severity
    title: str
    details: str = ""
    resolved: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


class HealthCheck(BaseModel):
    business_id: str
    website_up: bool = True
    pending_orders: int = 0
    low_stock_skus: int = 0
    failed_payments_24h: int = 0
    open_tickets: int = 0
    checked_at: datetime = Field(default_factory=utcnow)


class OperationsAgent(BaseAgent):
    agent_type = "operations"
    capabilities = [
        "health_monitoring",
        "incident_management",
        "alerting",
    ]
    description = "Monitors business operations and raises incidents."

    # Thresholds that trigger incidents.
    FAILED_PAYMENT_THRESHOLD = 5
    LOW_STOCK_THRESHOLD = 10
    TICKET_BACKLOG_THRESHOLD = 20

    def __init__(self) -> None:
        super().__init__()
        self._incidents: dict[str, Incident] = {}
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"inc_{self._counter:05d}"

    def check_health(
        self,
        business_id: str,
        website_up: bool = True,
        pending_orders: int = 0,
        low_stock_skus: int = 0,
        failed_payments_24h: int = 0,
        open_tickets: int = 0,
    ) -> list[Incident]:
        """Evaluate health signals; open incidents for threshold breaches."""
        raised: list[Incident] = []

        def _raise(category: str, severity: Severity, title: str, details: str = ""):
            inc = Incident(
                id=self._next_id(),
                business_id=business_id,
                category=category,
                severity=severity,
                title=title,
                details=details,
            )
            self._incidents[inc.id] = inc
            raised.append(inc)

        if not website_up:
            _raise("uptime", Severity.CRITICAL, "Website is down",
                   "Availability check failed.")
        if failed_payments_24h >= self.FAILED_PAYMENT_THRESHOLD:
            _raise("payment", Severity.CRITICAL,
                   f"{failed_payments_24h} failed payments in 24h",
                   "Possible payment provider issue.")
        if low_stock_skus >= self.LOW_STOCK_THRESHOLD:
            _raise("inventory", Severity.WARNING,
                   f"{low_stock_skus} SKUs low on stock")
        if open_tickets >= self.TICKET_BACKLOG_THRESHOLD:
            _raise("support", Severity.WARNING,
                   f"Support backlog: {open_tickets} open tickets")
        return raised

    def resolve(self, incident_id: str) -> Incident:
        inc = self._incidents.get(incident_id)
        if inc is None:
            raise ValueError(f"unknown incident: {incident_id}")
        inc.resolved = True
        inc.resolved_at = utcnow()
        return inc

    def open_incidents(self, business_id: str | None = None) -> list[Incident]:
        incidents = [i for i in self._incidents.values() if not i.resolved]
        if business_id:
            incidents = [i for i in incidents if i.business_id == business_id]
        return incidents

    def critical(self) -> list[Incident]:
        return [i for i in self.open_incidents()
                if i.severity == Severity.CRITICAL]

    def run(self, task: Task) -> dict:
        action = task.inputs.get("action", "report")
        if action == "report":
            open_ = self.open_incidents()
            self.record_usage(task, tokens=200, cost_usd=0.01)
            return {
                "open_incidents": len(open_),
                "critical": len(self.critical()),
                "by_category": {
                    c: sum(1 for i in open_ if i.category == c)
                    for c in {i.category for i in open_}
                },
            }
        raise ValueError(f"unknown action: {action}")
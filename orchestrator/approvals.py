"""Approval gates: decide whether an action needs human approval.

Thresholds are configurable. The default policy mirrors the spec:
ad spend is tiered, everything else consequential always needs a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Approval, ApprovalStatus, utcnow


@dataclass
class ApprovalPolicy:
    ad_spend_auto_max: float = 50.0
    ad_spend_approval_max: float = 250.0
    # Actions that always require human approval regardless of amount.
    always_require: list[str] = field(
        default_factory=lambda: [
            "open_financial_account",
            "move_funds",
            "change_payment_info",
            "sign_contract",
            "financial_commitment",
            "large_refund",
            "legal_response",
            "tax_configuration",
            "high_risk_marketplace_action",
            "change_security_settings",
            "delete_business",
            "delete_dataset",
        ]
    )


class ApprovalGate:
    def __init__(self, policy: ApprovalPolicy | None = None) -> None:
        self.policy = policy or ApprovalPolicy()
        self._approvals: dict[str, Approval] = {}

    def requires_approval(self, action: str, amount_usd: float | None = None) -> bool:
        if action in self.policy.always_require:
            return True
        if action == "ad_spend_increase":
            if amount_usd is None:
                return True
            return amount_usd > self.policy.ad_spend_auto_max
        # Unknown consequential actions default to requiring approval.
        return True

    def requires_secondary(self, action: str, amount_usd: float | None = None) -> bool:
        if action == "ad_spend_increase" and amount_usd is not None:
            return amount_usd > self.policy.ad_spend_approval_max
        return False

    def request(
        self,
        action: str,
        details: dict | None = None,
        amount_usd: float | None = None,
        task_id: str | None = None,
        business_id: str | None = None,
        requested_by: str = "orchestrator",
    ) -> Approval:
        approval = Approval(
            task_id=task_id,
            business_id=business_id,
            action=action,
            details=details or {},
            amount_usd=amount_usd,
            requested_by=requested_by,
        )
        self._approvals[approval.id] = approval
        return approval

    def decide(
        self, approval_id: str, approved: bool, decided_by: str, note: str | None = None
    ) -> Approval:
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise ValueError(f"unknown approval: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError(f"approval {approval_id} already decided")
        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        approval.decided_by = decided_by
        approval.decided_at = utcnow()
        approval.note = note
        return approval

    def pending(self) -> list[Approval]:
        return [a for a in self._approvals.values() if a.status == ApprovalStatus.PENDING]

    def get(self, approval_id: str) -> Approval | None:
        return self._approvals.get(approval_id)

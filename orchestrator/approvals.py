"""Approval gates: decide whether an action needs human approval.

Thresholds are configurable. The default policy mirrors the spec:
ad spend is tiered, everything else consequential always needs a human.

Persistence (opt-in): pass persist_path to share approvals across
processes — e.g. a mission script that exits and the dashboard GUI.
Every mutation is saved atomically (temp file + rename) with a
monotonic revision counter; reads refresh from disk when another
process wrote. Concurrent writes merge by approval id (no approval is
lost); on direct conflict the in-memory entry wins. Safe for the
single-user case; not a multi-writer database.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

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
    def __init__(self, policy: ApprovalPolicy | None = None,
                 persist_path: str | Path | None = None) -> None:
        self.policy = policy or ApprovalPolicy()
        self._approvals: dict[str, Approval] = {}
        # Decisions may arrive from the dashboard thread while the
        # dispatcher polls pending approvals on its own thread.
        self._lock = threading.RLock()
        self._persist_path = Path(persist_path) if persist_path else None
        self._rev = 0
        if self._persist_path is not None:
            self._refresh()

    # -- persistence ----------------------------------------------------

    def _read_file(self) -> tuple[int, dict[str, Approval]]:
        """Read (rev, approvals) from disk; tolerate missing/corrupt files."""
        try:
            raw = json.loads(self._persist_path.read_text())  # type: ignore[union-attr]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return 0, {}
        try:
            rev = int(raw.get("rev", 0))
            items = {k: Approval(**v) for k, v in
                     raw.get("approvals", {}).items()}
        except Exception:  # noqa: BLE001 - corrupt payload reads as empty
            return 0, {}
        return rev, items

    def _save(self) -> None:
        """Merge in-memory state into the file and write atomically."""
        if self._persist_path is None:
            return
        with self._lock:
            file_rev, disk = self._read_file()
            for aid, appr in self._approvals.items():
                disk[aid] = appr  # in-memory wins on direct conflict
            rev = max(self._rev, file_rev) + 1
            payload = {
                "rev": rev,
                "approvals": {k: v.model_dump(mode="json")
                              for k, v in disk.items()},
            }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_name(
                self._persist_path.name + ".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self._persist_path)
            self._approvals = disk
            self._rev = rev

    def _refresh(self) -> None:
        """Reload from disk when another process wrote since our last sync.

        Safe to call wholesale: every mutation in this process saves
        synchronously before returning, so any difference on disk came
        from another process and disk is authoritative for it.
        """
        if self._persist_path is None:
            return
        with self._lock:
            file_rev, disk = self._read_file()
            if file_rev > self._rev:
                self._approvals = disk
                self._rev = file_rev

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
        with self._lock:
            self._approvals[approval.id] = approval
        self._save()
        return approval

    def decide(
        self, approval_id: str, approved: bool, decided_by: str, note: str | None = None
    ) -> Approval:
        with self._lock:
            self._refresh()
            approval = self._approvals.get(approval_id)
            if approval is None:
                raise ValueError(f"unknown approval: {approval_id}")
            if approval.status != ApprovalStatus.PENDING:
                raise ValueError(f"approval {approval_id} already decided")
            approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            approval.decided_by = decided_by
            approval.decided_at = utcnow()
            approval.note = note
        self._save()
        return approval

    def pending(self) -> list[Approval]:
        self._refresh()
        with self._lock:
            return [a for a in self._approvals.values() if a.status == ApprovalStatus.PENDING]

    def decided(self, limit: int = 20) -> list[Approval]:
        """Recently decided approvals, newest first."""
        self._refresh()
        with self._lock:
            done = [a for a in self._approvals.values()
                    if a.status != ApprovalStatus.PENDING]
        done.sort(key=lambda a: (a.decided_at is None, a.decided_at),
                  reverse=True)
        return done[:limit]

    def all(self) -> list[Approval]:
        """Every approval, oldest first (consumers act on decided ones)."""
        self._refresh()
        with self._lock:
            items = list(self._approvals.values())
        items.sort(key=lambda a: a.requested_at)
        return items

    def get(self, approval_id: str) -> Approval | None:
        self._refresh()
        with self._lock:
            return self._approvals.get(approval_id)

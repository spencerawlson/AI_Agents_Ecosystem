"""Server-side plan entitlement checks for Axionory's new tiers.

Checked here, not only in the UI — a direct API request must be
refused the same as a blocked button. Legacy accounts (plan == "")
are never limited by anything in this module; workspaces, team seats,
and the rest are new-tier concepts only.
"""

from __future__ import annotations

from ventoryn.plans import PLANS, limit


def can_create_workspace(store, account: dict) -> tuple[bool, str]:
    """True when ``account`` can create one more workspace under its
    plan's limit. Always True for a legacy account."""
    plan = account.get("plan") or ""
    cap = limit(plan, "workspaces")
    if cap is None:
        return True, ""
    current = len(store.list_workspaces(account["id"]))
    if current >= cap:
        label = PLANS[plan]["label"]
        return False, (
            f"Your {label} plan allows {cap} workspace"
            f"{'s' if cap != 1 else ''}; you have {current}. "
            "Upgrade to create another.")
    return True, ""

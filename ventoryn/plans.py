"""Axionory's three subscription tiers (Starter/Growth/Pro).

Business model: the subscription buys access only (workspaces, agent
and campaign slots, team seats, feature tiers, API access) — it
includes NO usage credits on any tier. Every billable agent action is
funded 100% from the account's prepaid wallet (ventoryn/saas.py's
wallet_* methods), regardless of plan. This is deliberately different
from the legacy $20/month flat-credit plan (plan == ""), which keeps
its own self-contained monthly-allowance model, completely untouched
and unaffected by anything here — the two coexist; a legacy account
is never migrated onto a tier automatically.

Limits here are the INITIAL defaults, adjustable without code changes
by editing PLANS (a later pass can move this to a DB-backed config
table if the operator needs to tune it without a deploy).
"""

from __future__ import annotations

#: One-time wallet credit granted on first subscription to ANY new
#: tier (never for the legacy plan, never more than once — see
#: SaasStore.grant_starter_wallet_credit_once).
STARTER_WALLET_CREDIT_CENTS = 2500  # $25

#: Operator credit grants (admin assigns spendable balance to an
#: account — see SaasStore.create_operator_grant). A grant is made
#: in the currency the account spends: legacy credits for a no-plan
#: account, wallet cents for a tier account. Defaults: 50 legacy
#: credits or $5.00 wallet per month — "a few credits per month"
#: (Spencer, 2026-11-17).
OPERATOR_GRANT_DEFAULT_CREDITS = 50
OPERATOR_GRANT_DEFAULT_WALLET_CENTS = 500  # $5.00
#: One-time / monthly amount bounds. Wallet bounds mirror the
#: top-up bounds on SaasConfig (wallet_topup_min_cents/max_cents);
#: legacy grants are capped at the monthly allowance.
OPERATOR_GRANT_MIN_CREDITS = 1
OPERATOR_GRANT_MAX_CREDITS = 1000
OPERATOR_GRANT_MIN_WALLET_CENTS = 500  # $5.00
OPERATOR_GRANT_MAX_WALLET_CENTS = 50000  # $500.00
#: Platform-wide monthly cap on grants applied in a month
#: (Spencer, 2026-11-17: $100/month total). The two currencies
#: cannot be summed, so each has its own cap: wallet grants are
#: capped in dollars, legacy-credit grants in credits (5000 = one
#: hundred default monthly grants). Both stop runaway monthly
#: outflow; the cap is enforced in apply_due_monthly_grant.
OPERATOR_GRANT_MONTHLY_CAP_CENTS = 10000  # $100
OPERATOR_GRANT_MONTHLY_CAP_CREDITS = 5000

PLANS: dict[str, dict] = {
    "starter": {
        "label": "Starter",
        "price_usd": 20,
        "workspaces": 1,
        "active_agents": 2,
        "active_campaigns": 2,
        "basic_analytics": True,
        "advanced_analytics": False,
        # Starter posts to X (Spencer, 2026-11-13): a tier that could
        # not publish anywhere would be worth less than the legacy plan
        # at the same price. Facebook/Instagram need Growth, TikTok Pro.
        "premium_integrations": "x",
        "team_seats": 1,
        "api_access": False,
    },
    "growth": {
        "label": "Growth",
        "price_usd": 39,
        "workspaces": 3,
        "active_agents": 5,
        "active_campaigns": 10,
        "basic_analytics": True,
        "advanced_analytics": True,
        "premium_integrations": "selected",
        "team_seats": 3,
        "api_access": "limited",
    },
    "pro": {
        "label": "Pro",
        "price_usd": 99,
        "workspaces": 10,
        "active_agents": 15,
        "active_campaigns": 50,
        "basic_analytics": True,
        "advanced_analytics": True,
        "premium_integrations": "all",
        "team_seats": 10,
        "api_access": True,
    },
}

PLAN_NAMES = tuple(PLANS)


def plan_limits(plan: str) -> dict | None:
    """The tier's limits, or None for the legacy plan (plan == '') or
    an unrecognized value — callers must treat None as "no new-tier
    limits apply", never as "unlimited" by accident."""
    return PLANS.get(plan)


def limit(plan: str, key: str):
    """One limit from a plan, or None when the plan is legacy/unknown
    or does not define that key."""
    tier = plan_limits(plan)
    return tier.get(key) if tier else None


def is_wallet_account(account: dict) -> bool:
    """True for any new-tier account (billed from the prepaid wallet);
    False for a legacy flat-credit account (plan == '')."""
    return bool(account.get("plan"))

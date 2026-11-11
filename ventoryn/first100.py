"""The Founding Customer summary — how close a site is to the
Founding Customer goal of 100 paying customers.

Every figure comes from the store; nothing is invented. With nothing
verified the summary reads verified 0, remaining 100.

* "verified" — distinct verified paying customers
  (SaasStore.verified_customer_count: real-mode Stripe events with
  an identity; the owner-typed manual fallback never counts).
* "paying" — all recorded paying customers
  (SaasStore.paying_customer_count, funnel count, unchanged).
* "attributed" — recorded sales carrying Ventoryn's signature.
* "unattributed" — real Stripe sales with no signature (never
  guessed into attribution). Owner-recorded manual sales are in
  neither bucket — they are pending, not attributed either way.
* "recent" — the latest conversion rows for the site.
"""

from __future__ import annotations

GOAL = 100


def first100_summary(store, site_id: str, recent_limit: int = 10) -> dict:
    """{"goal", "verified", "paying", "remaining", "attributed",
    "unattributed", "recent"} for one site. All numbers are read
    from the store — a site with no verified customers returns
    verified 0 and remaining 100."""
    verified = store.verified_customer_count(site_id)
    paying = store.paying_customer_count(site_id)
    attributed = 0
    unattributed = 0
    for conversion in store.list_conversions(site_id, limit=1000):
        if conversion.get("source") == "manual":
            # Owner-recorded sales are pending, not attributed either
            # way — they sit in neither bucket.
            continue
        if conversion.get("attribution") == "ventoryn":
            attributed += 1
        elif conversion.get("attribution") == "unattributed":
            unattributed += 1
    return {
        "goal": GOAL,
        "verified": verified,
        "paying": paying,
        "remaining": max(0, GOAL - verified),
        "attributed": attributed,
        "unattributed": unattributed,
        "recent": store.list_conversions(site_id, limit=recent_limit),
    }

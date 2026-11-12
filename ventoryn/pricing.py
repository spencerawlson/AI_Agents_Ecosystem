"""Axionory Phase 5: price what Phase 4 recorded, and settle it
against the wallet reservation opened before the job ran.

Phase 4 (ventoryn/usage_metering.py) records every LLM call made
during a job into saas_usage_events, with provider_cost_cents filled
in but customer_charge_cents/pricing_version left unset. This module
is the policy layer that fills those in — a markup over the real
provider cost — and reconciles the result against the wallet
reservation ventoryn/autopilot.py and ventoryn/saas_routes.py open
before a wallet-funded job starts.

Legacy flat-credit accounts (plan == '') never reserve a wallet hold
and never call anything here.
"""

from __future__ import annotations

import math

#: Stamped onto every priced event, so a future repricing scheme can
#: coexist with historical data instead of silently reinterpreting it.
PRICING_VERSION = "v1"

#: Applied to provider_cost_cents, rounded up — the platform should
#: never lose a cent to rounding down a customer's charge.
MARKUP_MULTIPLIER = 1.5

#: Conservative per-job-type estimate (cents) used to size the wallet
#: hold placed before a job runs — its real cost is only known after
#: usage_metering records the job's actual LLM calls. Placeholder
#: values, not derived from real usage data yet; revisit once Phase 4
#: has accumulated enough saas_usage_events history to base these on
#: actual per-job-type averages instead.
WALLET_RESERVE_ESTIMATE_CENTS: dict[str, int] = {
    "analyze": 50,
    "x_campaign": 40,
    "geo_audit": 50,
    "community_listening": 60,
    "social_report": 40,
    "seo_article": 150,
    "video_render": 200,
}

#: Used for any job type not listed above.
DEFAULT_WALLET_RESERVE_ESTIMATE_CENTS = 100


def wallet_reserve_estimate_cents(job_type: str) -> int:
    return WALLET_RESERVE_ESTIMATE_CENTS.get(
        job_type, DEFAULT_WALLET_RESERVE_ESTIMATE_CENTS)


def price_cents(provider_cost_cents: int) -> int:
    """The customer charge for one unit of provider cost: the markup,
    rounded up."""
    return math.ceil(provider_cost_cents * MARKUP_MULTIPLIER)


def price_and_settle_job(store, job_id: str, reservation_id: str) -> int:
    """Price every unpriced saas_usage_events row for ``job_id``, then
    settle the wallet reservation opened for that job for the real
    (priced) total — refunding the unused part of the estimate
    automatically (settle_wallet_reservation's existing behavior). If
    the job recorded no billable usage at all, release the reservation
    instead of settling for 0.

    Idempotent: an event with customer_charge_cents already set is not
    re-priced, just re-summed, and settle/release are themselves
    no-ops on an already-resolved reservation — so calling this twice
    for the same job (e.g. a retried persist, or expire_stale_jobs
    resolving a job already settled by its own finally block) never
    double-charges. Returns the total charged, in cents."""
    total = 0
    for event in store.usage_events_for_job(job_id):
        if event["customer_charge_cents"] is None:
            charge = price_cents(int(event["provider_cost_cents"]))
            store.price_usage_event(event["id"], charge, PRICING_VERSION)
        else:
            charge = int(event["customer_charge_cents"])
        total += charge
    if total == 0:
        store.release_wallet_reservation(reservation_id)
    else:
        store.settle_wallet_reservation(
            reservation_id, total, f"job {job_id} usage")
    return total

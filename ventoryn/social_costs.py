"""Social publishing rate card and spend caps.

Prices and limits here are REPORTED, not directly verified against
the providers' official pages — the three verification docs
(docs/implementation/X_API_VERIFICATION.md,
FACEBOOK_API_VERIFICATION.md, INSTAGRAM_API_VERIFICATION.md) are
drafts built from secondary reporting, and a live-browser re-check
is still owed before any figure is quoted to a customer. The notes
in RATE_CARD say so in plain words, and the estimate is shown to the
customer BEFORE approval so nothing is ever charged by surprise.

Spend is recorded ONLY on a confirmed PUBLISHED post — the
Facebook/Instagram approve-and-post paths call
store.record_social_spend (ventoryn/saas.py) only after the
provider confirms the post; a failed, ambiguous, or
cap-blocked publish charges nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Defaults (USD). Env-overridable via SaasConfig:
# VENTORYN_SOCIAL_DAILY_CAP_USD, VENTORYN_SOCIAL_ACCOUNT_MONTHLY_CAP_USD,
# VENTORYN_SOCIAL_SITE_MONTHLY_CAP_USD.
DEFAULT_DAILY_CAP_USD = 10.00
DEFAULT_ACCOUNT_MONTHLY_CAP_USD = 50.00
DEFAULT_SITE_MONTHLY_CAP_USD = 25.00

#: Per-provider price of one link post, in USD. The "note" is shown
#: wherever the estimate is shown — it must never be dropped, because
#: it is the honesty label on the number.
RATE_CARD: dict[str, dict] = {
    "x": {
        "link_post_usd": 0.20,
        "note": ("REPORTED $0.20 per post with a link on X pay-per-use "
                 "(verification doc — not directly verified against X's "
                 "docs; confirm in the X developer console before quoting)"),
    },
    "facebook": {
        "link_post_usd": 0.0,
        "note": ("No per-call price found; Meta Graph API calls reported "
                 "$0 (verification doc, unverified — do not quote $0 to a "
                 "customer)"),
    },
    "instagram": {
        "link_post_usd": 0.0,
        "note": ("No per-call price found; Meta Graph API calls reported "
                 "$0 (verification doc, unverified — do not quote $0 to a "
                 "customer)"),
    },
}


def estimate_cost(provider: str, has_link: bool = True) -> float:
    """The estimated provider cost in USD of publishing one post.
    A post without a link is estimated at $0.00 on every provider
    (the reported X charge applies to posts carrying a link). Unknown
    providers raise ValueError — an estimate must never be guessed."""
    if provider not in RATE_CARD:
        raise ValueError(f"Unknown social provider: {provider}")
    if not has_link:
        return 0.0
    return float(RATE_CARD[provider]["link_post_usd"])


def rate_note(provider: str) -> str:
    """The honesty note for a provider's rate (see RATE_CARD)."""
    if provider not in RATE_CARD:
        raise ValueError(f"Unknown social provider: {provider}")
    return RATE_CARD[provider]["note"]


def _start_of_today_utc(now: datetime) -> str:
    return now.replace(hour=0, minute=0, second=0,
                       microsecond=0).isoformat()


def _start_of_month_utc(now: datetime) -> str:
    return now.replace(day=1, hour=0, minute=0, second=0,
                       microsecond=0).isoformat()


def caps_allow(store, account_id: str, site_id: str, amount_usd: float,
               config=None, now: datetime | None = None
               ) -> tuple[bool, str]:
    """True when a publish of `amount_usd` fits every cap, else False
    with a plain-language reason. Caps (USD):

    * global daily cap — all accounts together, this UTC day;
    * per-account monthly cap — this account, this UTC month;
    * per-site monthly cap — this site, this UTC month.

    `config` is a SaasConfig (defaults are read from the environment
    when it is None). `now` is injectable for tests. A cap breach
    blocks the publish and charges nothing."""
    from ventoryn.saas import SaasConfig  # deferred: avoids a cycle

    cfg = config if config is not None else SaasConfig.from_env()
    now = now or datetime.now(timezone.utc)
    amount = float(amount_usd)

    daily_cap = float(getattr(cfg, "social_daily_cap_usd",
                              DEFAULT_DAILY_CAP_USD))
    daily_spent = store.social_spend_in_period(
        _start_of_today_utc(now))
    if daily_spent + amount > daily_cap:
        return False, (
            f"That post would take today's total social spend to "
            f"${daily_spent + amount:.2f}, over the "
            f"${daily_cap:.2f} daily limit. Try again tomorrow.")

    account_cap = float(getattr(cfg, "social_account_monthly_cap_usd",
                                DEFAULT_ACCOUNT_MONTHLY_CAP_USD))
    account_spent = store.social_spend_in_period(
        _start_of_month_utc(now), account_id=account_id)
    if account_spent + amount > account_cap:
        return False, (
            f"That post would take this account's social spend this "
            f"month to ${account_spent + amount:.2f}, over the "
            f"${account_cap:.2f} monthly limit.")

    site_cap = float(getattr(cfg, "social_site_monthly_cap_usd",
                             DEFAULT_SITE_MONTHLY_CAP_USD))
    site_spent = store.social_spend_in_period(
        _start_of_month_utc(now), site_id=site_id)
    if site_spent + amount > site_cap:
        return False, (
            f"That post would take this site's social spend this month "
            f"to ${site_spent + amount:.2f}, over the "
            f"${site_cap:.2f} monthly limit.")

    return True, "Within the social spend caps."

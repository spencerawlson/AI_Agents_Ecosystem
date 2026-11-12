"""Campaign cost estimates — shown BEFORE anything is posted.

Before a post goes out (and before planning a campaign), the owner
sees what the platform will charge. Today that is X: its API is
pay-per-use, billed per post.

Rates are X's REPORTED pay-per-use prices (docs/implementation/
X_API_VERIFICATION.md): every figure there is convergent secondary
reporting of X's pricing page, not a direct read — so every estimate
says "reported rate" and points at X's Developer Console as the source
of truth. Nothing here is a quote from X.

    plain post            $0.015
    post with a link      $0.20   (Axionory's signed attribution link
                                   makes every linked post this rate)

An estimate is arithmetic on those rates and on the website's real
recorded X spend this month; it never invents usage.
"""

from __future__ import annotations

import math

X_RATE_PLAIN_POST_USD = 0.015
X_RATE_LINKED_POST_USD = 0.20
X_RATE_SOURCE = ("X's reported pay-per-use rates — confirm in X's "
                 "Developer Console before relying on them")

MAX_DAYS = 365
MAX_POSTS_PER_DAY = 50


def x_post_cost_usd(with_link: bool) -> float:
    """What X is reported to charge for one post."""
    return X_RATE_LINKED_POST_USD if with_link else X_RATE_PLAIN_POST_USD


def x_post_cost_cents(with_link: bool) -> int:
    """Whole cents for the spend log — rounded UP, so the monthly cap
    is never under-counted ($0.015 records as 2 cents)."""
    return math.ceil(round(x_post_cost_usd(with_link) * 100, 6))


def estimate_x_campaign(*, posts_per_day: float, days: int, with_link: bool,
                        spent_this_month_usd: float,
                        monthly_budget_usd: float) -> dict:
    """Estimate an X campaign before it runs.

    Returns posts, per-post rate, total, the budget left this month, and
    how many of these posts that budget covers — so the owner sees
    up front whether the plan fits. Inputs are clamped to sane bounds."""
    days = max(1, min(int(days), MAX_DAYS))
    posts_per_day = max(0.0, min(float(posts_per_day), MAX_POSTS_PER_DAY))
    posts = int(round(posts_per_day * days))
    rate = x_post_cost_usd(with_link)
    total = round(posts * rate, 2)
    left = max(0.0, round(monthly_budget_usd - spent_this_month_usd, 2))
    # Whole tenths of a cent: 4.60 / 0.20 must be 23, not 22.999.
    affordable = (round(left * 1000) // round(rate * 1000)) if rate else posts
    return {
        "platform": "X",
        "posts": posts,
        "days": days,
        "posts_per_day": posts_per_day,
        "with_link": with_link,
        "rate_usd": rate,
        "total_usd": total,
        "monthly_budget_usd": round(monthly_budget_usd, 2),
        "spent_this_month_usd": round(spent_this_month_usd, 2),
        "budget_left_usd": left,
        "posts_budget_covers": min(posts, affordable),
        "fits_budget": total <= left,
        "source": X_RATE_SOURCE,
    }


def describe(est: dict) -> str:
    """One plain sentence for the estimate."""
    kind = "with a link" if est["with_link"] else "without a link"
    line = (f"{est['posts']} X posts {kind} over {est['days']} days "
            f"at ${est['rate_usd']:.3f} each: about ${est['total_usd']:.2f}.")
    if est["fits_budget"]:
        return line + (f" Fits this month's X budget (${est['budget_left_usd']:.2f} "
                       f"of ${est['monthly_budget_usd']:.2f} left).")
    return line + (f" That is more than the ${est['budget_left_usd']:.2f} left in "
                   f"this month's ${est['monthly_budget_usd']:.2f} X budget — it "
                   f"covers {est['posts_budget_covers']} of those posts.")

"""Campaign cost estimates, shown before anything is posted."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ventoryn.campaign_cost import (describe, estimate_x_campaign,
                                    x_post_cost_cents, x_post_cost_usd)


def test_thirty_day_linked_campaign_is_six_dollars():
    est = estimate_x_campaign(posts_per_day=1, days=30, with_link=True,
                              spent_this_month_usd=0, monthly_budget_usd=10)
    assert est["posts"] == 30 and est["total_usd"] == 6.00 and est["fits_budget"]
    assert "about $6.00" in describe(est) and "Fits" in describe(est)


def test_over_budget_says_how_many_posts_fit():
    est = estimate_x_campaign(posts_per_day=1, days=30, with_link=True,
                              spent_this_month_usd=0.40, monthly_budget_usd=5)
    assert not est["fits_budget"] and est["budget_left_usd"] == 4.60
    assert est["posts_budget_covers"] == 23
    assert "covers 23 of those posts" in describe(est)


def test_plain_posts_and_bounds():
    est = estimate_x_campaign(posts_per_day=2, days=30, with_link=False,
                              spent_this_month_usd=0, monthly_budget_usd=5)
    assert est["posts"] == 60 and est["total_usd"] == 0.90
    wild = estimate_x_campaign(posts_per_day=10_000, days=99_999, with_link=True,
                               spent_this_month_usd=0, monthly_budget_usd=5)
    assert wild["days"] == 365 and wild["posts_per_day"] == 50


def test_recorded_cost_rounds_up():
    assert x_post_cost_usd(True) == 0.20 and x_post_cost_cents(True) == 20
    assert x_post_cost_usd(False) == 0.015 and x_post_cost_cents(False) == 2

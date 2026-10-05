"""Margin-based pricing for physical products.

"60% profit" here means a 60% margin on the selling price AFTER the
landed product cost (unit + shipping to customer + any extras) and the
payment processor fee — not a 60% markup on cost.

    price = (landed_cost + fee_fixed) / (1 - target_margin - fee_pct)

Ad spend is NOT in the price: it is reported separately as the most an
order can afford in ads while still keeping a minimum net margin, so the
marketing stage knows its per-order ceiling.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

DEFAULT_TARGET_MARGIN = 0.60
DEFAULT_MIN_NET_MARGIN = 0.20


@dataclass(frozen=True)
class FeeSchedule:
    """Payment processing fee per order (Shopify Payments, Basic, US online)."""

    pct: float = 0.029
    fixed_usd: float = 0.30

    def fee_for(self, price_usd: float) -> float:
        return price_usd * self.pct + self.fixed_usd


@dataclass(frozen=True)
class PriceQuote:
    price_usd: float
    landed_cost_usd: float
    fees_usd: float
    profit_usd: float
    margin: float
    target_margin: float
    max_ad_cost_per_order_usd: float
    min_net_margin: float

    def to_dict(self) -> dict:
        return asdict(self)


def _charm_round(price: float) -> float:
    """Round UP to the next .99 ending so the margin never drops below target."""
    charm = math.ceil(price) - 0.01
    if charm + 1e-9 < price:
        charm += 1.0
    return round(charm, 2)


def quote(price_usd: float, landed_cost_usd: float,
          target_margin: float = DEFAULT_TARGET_MARGIN,
          fees: FeeSchedule = FeeSchedule(),
          min_net_margin: float = DEFAULT_MIN_NET_MARGIN) -> PriceQuote:
    """Break down the economics of selling at a given price."""
    if price_usd <= 0:
        raise ValueError("price must be positive")
    fee = fees.fee_for(price_usd)
    profit = price_usd - landed_cost_usd - fee
    max_ad = max(0.0, profit - min_net_margin * price_usd)
    return PriceQuote(
        price_usd=round(price_usd, 2),
        landed_cost_usd=round(landed_cost_usd, 2),
        fees_usd=round(fee, 2),
        profit_usd=round(profit, 2),
        margin=round(profit / price_usd, 4),
        target_margin=target_margin,
        max_ad_cost_per_order_usd=round(max_ad, 2),
        min_net_margin=min_net_margin,
    )


def price_for_margin(unit_cost_usd: float, shipping_cost_usd: float = 0.0,
                     other_cost_usd: float = 0.0,
                     target_margin: float = DEFAULT_TARGET_MARGIN,
                     fees: FeeSchedule = FeeSchedule(),
                     charm: bool = True,
                     min_net_margin: float = DEFAULT_MIN_NET_MARGIN) -> PriceQuote:
    """Lowest selling price that hits target_margin after costs and fees."""
    if unit_cost_usd < 0 or shipping_cost_usd < 0 or other_cost_usd < 0:
        raise ValueError("costs cannot be negative")
    if not 0 <= target_margin < 1:
        raise ValueError("target_margin must be in [0, 1)")
    denominator = 1 - target_margin - fees.pct
    if denominator <= 0:
        raise ValueError(
            f"target margin {target_margin:.0%} is impossible with a "
            f"{fees.pct:.1%} payment fee")
    landed = unit_cost_usd + shipping_cost_usd + other_cost_usd
    price = (landed + fees.fixed_usd) / denominator
    if charm:
        price = _charm_round(price)
    else:
        price = math.ceil(price * 100) / 100
    return quote(price, landed, target_margin=target_margin, fees=fees,
                 min_net_margin=min_net_margin)

"""Transparent profit arithmetic, in exact decimal.

The 5x resale multiple vs. "500% ROI" distinction
-------------------------------------------------
These are different quantities and conflating them overstates a deal by a
full turn of capital:

    buy at $100, resell at $500
      gross_multiple    = 500 / 100        = 5.0   -> a "5x flip"
      gross_roi_percent = (500 - 100) / 100 * 100  = 400%

A 5x resale multiple is **400% gross ROI before expenses**, not 500% ROI. The
$100 of capital comes back to you; only the other $400 is profit. To actually
earn 500% gross ROI you need a 6x multiple.

``qualifies_for_500_percent_resale_multiple`` therefore tests
``gross_multiple >= 5`` — the resale-multiple reading of "500% potential" —
and is deliberately kept as a separate field from ``gross_roi_percent`` so no
caller can accidentally read one as the other.

What this module does NOT do
----------------------------
It does not decide whether an item is a good buy. It performs arithmetic on a
``resale_price`` the caller supplies. Nothing here validates that the resale
price is achievable.

**An eBay Browse search returns CURRENT ASKING PRICES, not sold prices.**
Anyone can list a $12 item for $900; that listing proves only that someone
asked. A production resale estimator must derive ``resale_price`` from
authorized sold/completed comparable data — via eBay's Marketplace Insights
API (restricted access, application required) or another licensed sold-data
source — before it assigns a resale estimate or a confidence score. Feeding a
current asking price into this function produces arithmetic that is correct
and a conclusion that is worthless.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.models import ProfitAssumptions, ProfitEstimate

#: Threshold for the "500% potential" flag, read as a *resale multiple*.
FIVE_X_RESALE_MULTIPLE = Decimal("5")

_MONEY_QUANTUM = Decimal("0.01")
_RATIO_QUANTUM = Decimal("0.0001")
_PERCENT_QUANTUM = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    """Round to cents, half-up (the convention people expect for money)."""
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _ratio(value: Decimal) -> Decimal:
    return value.quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_UP)


def _percent(value: Decimal) -> Decimal:
    return value.quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)


def total_other_costs(assumptions: ProfitAssumptions) -> Decimal:
    """Sum every non-fee expense. Unrounded."""
    return (
        assumptions.shipping_cost
        + assumptions.taxes
        + assumptions.fuel_cost
        + assumptions.repair_cost
        + assumptions.cleaning_cost
        + assumptions.packaging_cost
        + assumptions.other_costs
    )


def total_selling_fees(assumptions: ProfitAssumptions) -> Decimal:
    """Marketplace + payment fees as a share of the resale price. Unrounded."""
    return assumptions.resale_price * (
        assumptions.marketplace_fee_rate + assumptions.payment_fee_rate
    )


def estimate_profit(assumptions: ProfitAssumptions) -> ProfitEstimate:
    """Compute a fully itemized profit estimate.

    ``purchase_price > 0`` is enforced by :class:`ProfitAssumptions`, so every
    division below is safe and no zero-guard branch is needed.

    Rounding policy: fees and other costs are rounded to cents *first*, and
    ``net_profit`` is then computed from those rounded components. That way the
    numbers in the response add up exactly as displayed — a caller can check
    the arithmetic on paper and get the same answer. Ratios are derived from
    the same rounded figures for the same reason.
    """
    resale = assumptions.resale_price
    purchase = assumptions.purchase_price

    fees = _money(total_selling_fees(assumptions))
    other = _money(total_other_costs(assumptions))

    gross_profit = _money(resale - purchase)
    net_profit = _money(resale - purchase - fees - other)

    # The qualification test uses the UNROUNDED multiple. Rounding first could
    # promote 4.99996x to "5.0000" and flag a deal that does not clear the bar.
    exact_multiple = resale / purchase
    qualifies = exact_multiple >= FIVE_X_RESALE_MULTIPLE

    # Denominator is total capital at risk: purchase + fees + other costs.
    # purchase > 0 and the rest are >= 0, so this is always positive.
    net_cost_basis = purchase + fees + other

    return ProfitEstimate(
        resale_price=_money(resale),
        purchase_price=_money(purchase),
        gross_profit=gross_profit,
        total_selling_fees=fees,
        total_other_costs=other,
        net_profit=net_profit,
        gross_multiple=_ratio(exact_multiple),
        gross_roi_percent=_percent(gross_profit / purchase * Decimal("100")),
        net_roi_percent=_percent(net_profit / net_cost_basis * Decimal("100")),
        qualifies_for_500_percent_resale_multiple=qualifies,
    )

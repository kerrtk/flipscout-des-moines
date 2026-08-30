"""Profit arithmetic, and the 5x-multiple vs 500%-ROI distinction."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models import ProfitAssumptions
from app.services.profitability import estimate_profit


def assumptions(**overrides) -> ProfitAssumptions:
    """A zero-fee, zero-cost baseline so each test isolates one variable."""
    base = {
        "resale_price": Decimal("500"),
        "purchase_price": Decimal("100"),
        "marketplace_fee_rate": Decimal("0"),
        "payment_fee_rate": Decimal("0"),
    }
    base.update(overrides)
    return ProfitAssumptions(**base)


# --------------------------------------------------------------------------- #
# 7. Exactly 5x qualifies
# --------------------------------------------------------------------------- #


def test_exactly_five_times_qualifies() -> None:
    """The threshold is inclusive: exactly 5.0x clears the bar."""
    estimate = estimate_profit(
        assumptions(resale_price=Decimal("500"), purchase_price=Decimal("100"))
    )
    assert estimate.gross_multiple == Decimal("5.0000")
    assert estimate.qualifies_for_500_percent_resale_multiple is True


@pytest.mark.parametrize(
    ("purchase", "resale"),
    [
        (Decimal("1"), Decimal("5")),
        (Decimal("12.50"), Decimal("62.50")),
        (Decimal("3.33"), Decimal("16.65")),
        (Decimal("0.01"), Decimal("0.05")),
    ],
)
def test_exact_five_multiple_qualifies_at_any_scale(purchase, resale) -> None:
    estimate = estimate_profit(assumptions(resale_price=resale, purchase_price=purchase))
    assert estimate.gross_multiple == Decimal("5.0000")
    assert estimate.qualifies_for_500_percent_resale_multiple is True


def test_above_five_times_qualifies() -> None:
    estimate = estimate_profit(
        assumptions(resale_price=Decimal("501"), purchase_price=Decimal("100"))
    )
    assert estimate.qualifies_for_500_percent_resale_multiple is True


# --------------------------------------------------------------------------- #
# 8. Less than 5x does not qualify
# --------------------------------------------------------------------------- #


def test_just_under_five_times_does_not_qualify() -> None:
    estimate = estimate_profit(
        assumptions(resale_price=Decimal("499.99"), purchase_price=Decimal("100"))
    )
    assert estimate.gross_multiple == Decimal("4.9999")
    assert estimate.qualifies_for_500_percent_resale_multiple is False


def test_qualification_uses_the_unrounded_multiple() -> None:
    """4.99996x rounds to 5.0000 for display but must NOT qualify.

    Guards the ordering bug where a deal is flagged because the *displayed*
    multiple was rounded up before the comparison.
    """
    estimate = estimate_profit(
        assumptions(resale_price=Decimal("499.996"), purchase_price=Decimal("100"))
    )
    assert estimate.gross_multiple == Decimal("5.0000")  # display rounds up
    assert estimate.qualifies_for_500_percent_resale_multiple is False  # test does not


@pytest.mark.parametrize(
    "resale", [Decimal("100"), Decimal("200"), Decimal("400"), Decimal("499")]
)
def test_multiples_below_five_do_not_qualify(resale) -> None:
    estimate = estimate_profit(
        assumptions(resale_price=resale, purchase_price=Decimal("100"))
    )
    assert estimate.qualifies_for_500_percent_resale_multiple is False


# --------------------------------------------------------------------------- #
# 9. Gross multiple vs gross ROI
# --------------------------------------------------------------------------- #


def test_five_x_multiple_is_four_hundred_percent_gross_roi() -> None:
    """The core distinction: a 5x flip is 400% ROI, not 500% ROI.

    Buy $100, sell $500: $100 of capital returns to you and $400 is profit.
    """
    estimate = estimate_profit(
        assumptions(resale_price=Decimal("500"), purchase_price=Decimal("100"))
    )
    assert estimate.gross_multiple == Decimal("5.0000")
    assert estimate.gross_roi_percent == Decimal("400.00")
    assert estimate.gross_roi_percent != Decimal("500.00")
    assert estimate.qualifies_for_500_percent_resale_multiple is True


def test_five_hundred_percent_gross_roi_requires_a_six_x_multiple() -> None:
    """The converse: 500% ROI is a 6x multiple, not a 5x one."""
    estimate = estimate_profit(
        assumptions(resale_price=Decimal("600"), purchase_price=Decimal("100"))
    )
    assert estimate.gross_roi_percent == Decimal("500.00")
    assert estimate.gross_multiple == Decimal("6.0000")


def test_roi_is_always_one_hundred_points_below_the_multiple_percentage() -> None:
    """gross_roi_percent == (gross_multiple * 100) - 100, by definition."""
    for resale in (Decimal("150"), Decimal("500"), Decimal("1000")):
        estimate = estimate_profit(
            assumptions(resale_price=resale, purchase_price=Decimal("100"))
        )
        assert estimate.gross_roi_percent == (
            estimate.gross_multiple * Decimal("100") - Decimal("100")
        ).quantize(Decimal("0.01"))


def test_a_deal_can_qualify_on_multiple_while_net_profit_is_negative() -> None:
    """Qualifying on the 5x multiple is not a claim of profitability.

    Buy $10, sell $50 (5x), but spend $45 on fees and expenses -> a loss.
    """
    estimate = estimate_profit(
        ProfitAssumptions(
            resale_price=Decimal("50"),
            purchase_price=Decimal("10"),
            marketplace_fee_rate=Decimal("0.13"),
            payment_fee_rate=Decimal("0"),
            shipping_cost=Decimal("20"),
            repair_cost=Decimal("15"),
            fuel_cost=Decimal("10"),
        )
    )
    assert estimate.qualifies_for_500_percent_resale_multiple is True
    assert estimate.gross_profit == Decimal("40.00")
    assert estimate.net_profit < 0
    assert estimate.net_roi_percent < 0


# --------------------------------------------------------------------------- #
# 10. Calculations with fees and expenses
# --------------------------------------------------------------------------- #


def test_full_calculation_with_fees_and_expenses() -> None:
    """A worked example every figure of which can be checked by hand."""
    estimate = estimate_profit(
        ProfitAssumptions(
            resale_price=Decimal("200.00"),
            purchase_price=Decimal("40.00"),
            marketplace_fee_rate=Decimal("0.1325"),
            payment_fee_rate=Decimal("0.0299"),
            shipping_cost=Decimal("12.00"),
            taxes=Decimal("2.80"),
            fuel_cost=Decimal("6.00"),
            repair_cost=Decimal("5.00"),
            cleaning_cost=Decimal("3.00"),
            packaging_cost=Decimal("2.20"),
            other_costs=Decimal("1.00"),
        )
    )

    # fees = 200 * (0.1325 + 0.0299) = 200 * 0.1624 = 32.48
    assert estimate.total_selling_fees == Decimal("32.48")
    # other = 12 + 2.80 + 6 + 5 + 3 + 2.20 + 1 = 32.00
    assert estimate.total_other_costs == Decimal("32.00")
    # gross = 200 - 40 = 160
    assert estimate.gross_profit == Decimal("160.00")
    # net = 200 - 40 - 32.48 - 32.00 = 95.52
    assert estimate.net_profit == Decimal("95.52")
    # multiple = 200 / 40 = 5
    assert estimate.gross_multiple == Decimal("5.0000")
    assert estimate.qualifies_for_500_percent_resale_multiple is True
    # gross ROI = 160 / 40 * 100 = 400%
    assert estimate.gross_roi_percent == Decimal("400.00")
    # net ROI = 95.52 / (40 + 32.48 + 32) * 100 = 95.52 / 104.48 * 100 = 91.42%
    assert estimate.net_roi_percent == Decimal("91.42")


def test_response_components_add_up_exactly_as_displayed() -> None:
    """net_profit must reconcile against the other returned figures."""
    estimate = estimate_profit(
        ProfitAssumptions(
            resale_price=Decimal("99.99"),
            purchase_price=Decimal("17.33"),
            marketplace_fee_rate=Decimal("0.1325"),
            payment_fee_rate=Decimal("0.029"),
            shipping_cost=Decimal("9.87"),
            taxes=Decimal("1.11"),
        )
    )
    assert estimate.net_profit == (
        estimate.resale_price
        - estimate.purchase_price
        - estimate.total_selling_fees
        - estimate.total_other_costs
    )
    assert estimate.gross_profit == estimate.resale_price - estimate.purchase_price


def test_zero_fee_zero_cost_net_equals_gross() -> None:
    estimate = estimate_profit(assumptions())
    assert estimate.net_profit == estimate.gross_profit
    assert estimate.total_selling_fees == Decimal("0.00")
    assert estimate.total_other_costs == Decimal("0.00")
    assert estimate.net_roi_percent == Decimal("400.00")


def test_defaults_cover_every_optional_cost() -> None:
    """Only resale_price and purchase_price are required."""
    estimate = estimate_profit(
        ProfitAssumptions(resale_price=Decimal("100"), purchase_price=Decimal("25"))
    )
    assert estimate.total_other_costs == Decimal("0.00")
    # Default 13.25% marketplace fee, 0% payment fee (eBay bundles processing).
    assert estimate.total_selling_fees == Decimal("13.25")
    assert estimate.net_profit == Decimal("61.75")


def test_arithmetic_is_exact_decimal_not_binary_float() -> None:
    """0.1 + 0.2 != 0.3 in binary float; this must come out exact."""
    estimate = estimate_profit(
        assumptions(
            resale_price=Decimal("1.00"),
            purchase_price=Decimal("0.10"),
            shipping_cost=Decimal("0.10"),
            taxes=Decimal("0.20"),
        )
    )
    assert estimate.total_other_costs == Decimal("0.30")
    assert estimate.net_profit == Decimal("0.60")


def test_loss_making_deal_reports_negative_figures() -> None:
    estimate = estimate_profit(
        assumptions(resale_price=Decimal("50"), purchase_price=Decimal("100"))
    )
    assert estimate.gross_profit == Decimal("-50.00")
    assert estimate.net_profit == Decimal("-50.00")
    assert estimate.gross_roi_percent == Decimal("-50.00")
    assert estimate.qualifies_for_500_percent_resale_multiple is False


def test_zero_resale_price_is_allowed_and_yields_total_loss() -> None:
    estimate = estimate_profit(
        assumptions(resale_price=Decimal("0"), purchase_price=Decimal("20"))
    )
    assert estimate.gross_multiple == Decimal("0.0000")
    assert estimate.net_profit == Decimal("-20.00")


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("purchase", [Decimal("0"), Decimal("-1")])
def test_non_positive_purchase_price_is_rejected(purchase) -> None:
    """purchase_price is every ratio's denominator, so it must be > 0."""
    with pytest.raises(ValidationError):
        ProfitAssumptions(resale_price=Decimal("100"), purchase_price=purchase)


@pytest.mark.parametrize(
    "field",
    [
        "resale_price",
        "shipping_cost",
        "taxes",
        "fuel_cost",
        "repair_cost",
        "cleaning_cost",
        "packaging_cost",
        "other_costs",
    ],
)
def test_negative_money_is_rejected(field) -> None:
    # Built as one dict so the parametrized field overrides the baseline value
    # rather than colliding with it as a duplicate keyword argument.
    payload = {
        "resale_price": Decimal("100"),
        "purchase_price": Decimal("10"),
        field: Decimal("-1"),
    }
    with pytest.raises(ValidationError):
        ProfitAssumptions(**payload)


@pytest.mark.parametrize("rate", [Decimal("-0.01"), Decimal("1.01")])
def test_fee_rates_outside_zero_to_one_are_rejected(rate) -> None:
    with pytest.raises(ValidationError):
        ProfitAssumptions(
            resale_price=Decimal("100"),
            purchase_price=Decimal("10"),
            marketplace_fee_rate=rate,
        )


def test_combined_fee_rate_above_one_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ProfitAssumptions(
            resale_price=Decimal("100"),
            purchase_price=Decimal("10"),
            marketplace_fee_rate=Decimal("0.7"),
            payment_fee_rate=Decimal("0.4"),
        )


def test_unknown_field_is_rejected() -> None:
    """extra='forbid' catches typos instead of silently ignoring them."""
    with pytest.raises(ValidationError):
        ProfitAssumptions(
            resale_price=Decimal("100"),
            purchase_price=Decimal("10"),
            markteplace_fee_rate=Decimal("0.13"),
        )

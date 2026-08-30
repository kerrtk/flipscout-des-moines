"""Storage-unit auction economics - where the hidden costs live."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app.services.storage_auctions import (
    StorageUnitAuction,
    evaluate_storage_unit,
    format_storage_estimate,
)


def unit(**overrides) -> StorageUnitAuction:
    fields = {
        "facility": "Test Facility",
        "city": "Des Moines",
        "state": "IA",
        "bid": Decimal("100"),
        "estimated_contents_value": Decimal("2000"),
        "sellable_fraction": Decimal("0.3"),
        "buyers_premium_rate": Decimal("0.15"),
        "sales_tax_rate": Decimal("0.07"),
        "round_trip_miles": Decimal("40"),
        "loads_required": 1,
        "disposal_cost": Decimal("100"),
        "labor_hours": Decimal("6"),
    }
    fields.update(overrides)
    return StorageUnitAuction(**fields)


def test_buyers_premium_and_tax_are_charged_on_top_of_the_bid() -> None:
    """A $100 hammer price is never a $100 cost."""
    estimate = evaluate_storage_unit(unit(bid=Decimal("100")))
    assert estimate.buyers_premium == Decimal("15.00")
    # Tax applies to bid + premium, not the bid alone.
    assert estimate.sales_tax == Decimal("8.05")


def test_total_cost_includes_every_component() -> None:
    estimate = evaluate_storage_unit(
        unit(), cost_per_mile=Decimal("0.65"), hourly_value_of_time=Decimal("25")
    )
    assert estimate.total_cost == (
        estimate.bid
        + estimate.buyers_premium
        + estimate.sales_tax
        + estimate.hauling_cost
        + estimate.disposal_cost
        + estimate.labor_cost
    )


def test_revenue_is_only_the_sellable_fraction() -> None:
    """You win the whole room; you can only sell part of it."""
    estimate = evaluate_storage_unit(
        unit(estimated_contents_value=Decimal("2000"), sellable_fraction=Decimal("0.3"))
    )
    assert estimate.expected_revenue == Decimal("600.00")


def test_a_unit_can_lose_money_despite_a_cheap_bid() -> None:
    """The classic trap: $250 bid, $2000 of contents, still a loss."""
    estimate = evaluate_storage_unit(
        unit(
            bid=Decimal("250"),
            round_trip_miles=Decimal("40"),
            loads_required=2,
            disposal_cost=Decimal("120"),
            labor_hours=Decimal("10"),
        )
    )
    assert estimate.expected_revenue == Decimal("600.00")
    assert estimate.net_profit < 0


def test_some_units_lose_money_even_when_free() -> None:
    """Hauling + disposal + labour alone can exceed the revenue."""
    estimate = evaluate_storage_unit(
        unit(
            bid=Decimal("0"),
            estimated_contents_value=Decimal("300"),
            sellable_fraction=Decimal("0.3"),
            round_trip_miles=Decimal("240"),
            disposal_cost=Decimal("60"),
            labor_hours=Decimal("6"),
        )
    )
    assert estimate.max_profitable_bid == Decimal("0.00")
    assert any("UNPROFITABLE AT ANY BID" in w for w in estimate.warnings)


def test_walk_away_bid_actually_breaks_even() -> None:
    """Bidding exactly the walk-away figure must land at ~zero profit."""
    base = unit(bid=Decimal("100"))
    walk_away = evaluate_storage_unit(base).max_profitable_bid
    at_limit = evaluate_storage_unit(unit(bid=walk_away))
    assert abs(at_limit.net_profit) <= Decimal("0.05")


def test_bidding_above_the_walk_away_figure_loses_money() -> None:
    walk_away = evaluate_storage_unit(unit()).max_profitable_bid
    over = evaluate_storage_unit(unit(bid=walk_away + Decimal("50")))
    assert over.net_profit < 0


def test_more_loads_cost_more_to_haul() -> None:
    one = evaluate_storage_unit(unit(loads_required=1))
    three = evaluate_storage_unit(unit(loads_required=3))
    assert three.hauling_cost == one.hauling_cost * 3
    assert three.net_profit < one.net_profit


def test_capital_required_includes_the_refundable_deposit() -> None:
    """The deposit comes back, but you still need it on the day."""
    estimate = evaluate_storage_unit(unit(cleaning_deposit=Decimal("150")))
    assert estimate.capital_required == estimate.total_cost + Decimal("150")


def test_clearout_is_infeasible_when_hours_fall_short() -> None:
    estimate = evaluate_storage_unit(
        unit(labor_hours=Decimal("10")),
        available_hours_before_deadline=Decimal("4"),
    )
    assert estimate.clearout_feasible is False
    assert any("forfeits" in w for w in estimate.warnings)


def test_clearout_feasible_with_enough_time() -> None:
    estimate = evaluate_storage_unit(
        unit(labor_hours=Decimal("5")),
        available_hours_before_deadline=Decimal("12"),
    )
    assert estimate.clearout_feasible is True


def test_unchecked_deadline_is_flagged_rather_than_assumed_fine() -> None:
    """Silence about the deadline would be the dangerous default."""
    estimate = evaluate_storage_unit(unit())
    assert any("unchecked" in w for w in estimate.warnings)


def test_optimistic_sellable_fraction_is_challenged() -> None:
    estimate = evaluate_storage_unit(unit(sellable_fraction=Decimal("0.8")))
    assert any("optimistic" in w for w in estimate.warnings)


def test_zero_disposal_cost_is_challenged() -> None:
    """Every unit has trash; dump fees are where margins die."""
    estimate = evaluate_storage_unit(unit(disposal_cost=Decimal("0")))
    assert any("disposal_cost is 0" in w for w in estimate.warnings)


def test_money_is_decimal_throughout() -> None:
    estimate = evaluate_storage_unit(unit())
    for value in (
        estimate.total_cost,
        estimate.net_profit,
        estimate.max_profitable_bid,
        estimate.capital_required,
    ):
        assert isinstance(value, Decimal)


def test_report_shows_the_walk_away_number() -> None:
    report = format_storage_estimate(evaluate_storage_unit(unit()))
    assert "WALK AWAY ABOVE" in report
    assert "cash needed" in report


@pytest.mark.parametrize(
    "bad",
    [
        {"sellable_fraction": Decimal("0")},
        {"sellable_fraction": Decimal("1.5")},
        {"bid": Decimal("-1")},
        {"estimated_contents_value": Decimal("-1")},
        {"loads_required": 0},
        {"clearout_deadline_hours": 0},
        {"state": "Iowa"},
    ],
)
def test_invalid_unit_input_is_rejected(bad) -> None:
    with pytest.raises(ValueError):
        unit(**bad)


def test_premium_plus_tax_above_one_hundred_percent_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot exceed 100%"):
        unit(buyers_premium_rate=Decimal("0.8"), sales_tax_rate=Decimal("0.3"))


def test_the_shipped_example_units_file_parses() -> None:
    raw = yaml.safe_load(
        (Path(__file__).parent.parent / "storage-units.example.yaml").read_text()
    )
    units = [StorageUnitAuction.model_validate(u) for u in raw["units"]]
    assert len(units) >= 2
    for parsed in units:
        evaluate_storage_unit(parsed)

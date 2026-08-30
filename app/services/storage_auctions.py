"""Storage-unit auction economics.

Why this is a separate module
-----------------------------
A storage unit is not a listing. You bid sight-unseen on a room you may only
look into from the doorway, you win *everything* in it including the trash,
and you must clear it completely inside a short window - usually 24 to 72
hours. The costs that decide profitability are almost all absent from the bid
price:

- a buyer's premium on top of the hammer price,
- sales tax,
- a refundable cleaning deposit that ties up cash either way,
- hauling, possibly several loads,
- **dump fees for the fraction that is worthless**, and
- your own hours sorting it.

Most people who lose money at storage auctions lose it on the last two. A unit
that is 70% junk can be a loss at a $50 bid, because you paid to haul and then
paid again to throw away.

A box truck is a genuine edge here: the clear-out deadline is what stops most
bidders, and hauling capacity is exactly what a truck solves.

Sourcing note
-------------
The major platforms - StorageTreasures, Lockerfox, Bid13, SelfStorageAuction -
publish no public API. This module therefore does **not** discover auctions.
It evaluates ones you have already found, through those sites' own saved
searches and email alerts, which is the supported way to be notified. Scraping
them would violate their terms, and this project does not do that.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.geo import Waypoint

_MONEY = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY, rounding=ROUND_HALF_UP)


class StorageUnitAuction(BaseModel):
    """One unit you are considering bidding on.

    Every field is an estimate you supply from looking at the photos or
    standing in the doorway. Nothing here is measured, and the output is only
    as good as ``sellable_fraction`` - the honest guess about how much of the
    room is worth anything.
    """

    model_config = ConfigDict(extra="forbid")

    facility: str
    city: str
    state: str = Field(min_length=2, max_length=2)
    location: Waypoint | None = None
    unit_size: str | None = Field(default=None, description="e.g. 10x20")

    auction_ends_at: datetime | None = None
    #: Hours after winning in which the unit must be emptied. Miss it and you
    #: forfeit the deposit and, usually, future bidding rights at that chain.
    clearout_deadline_hours: int = Field(default=48, gt=0, le=336)

    #: What you intend to bid, or the current high bid.
    bid: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    buyers_premium_rate: Decimal = Field(default=Decimal("0.15"), ge=0, le=1)
    sales_tax_rate: Decimal = Field(default=Decimal("0.07"), ge=0, le=1)
    #: Refundable if you leave the unit broom-clean. Not a cost, but it is
    #: cash you cannot spend until you get it back.
    cleaning_deposit: Decimal = Field(default=Decimal("100"), ge=0)

    #: Your estimate of what the WHOLE unit's contents would fetch if every
    #: sellable thing sold. Photos only - be conservative.
    estimated_contents_value: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    #: Share of the contents actually worth listing. The rest is dump weight.
    sellable_fraction: Decimal = Field(default=Decimal("0.3"), gt=0, le=1)

    #: Round-trip miles to clear it, times the number of loads.
    round_trip_miles: Decimal = Field(default=Decimal("0"), ge=0)
    loads_required: int = Field(default=1, ge=1, le=20)
    #: Landfill/transfer-station cost for the unsellable fraction.
    disposal_cost: Decimal = Field(default=Decimal("0"), ge=0)
    #: Hours to empty, sort, clean, and list.
    labor_hours: Decimal = Field(default=Decimal("6"), ge=0)

    notes: str | None = None

    @model_validator(mode="after")
    def _sanity(self) -> StorageUnitAuction:
        if self.buyers_premium_rate + self.sales_tax_rate > 1:
            raise ValueError("buyer's premium plus sales tax cannot exceed 100%")
        return self


class StorageUnitEstimate(BaseModel):
    """Full itemized cost and outcome for one unit. Nothing hidden."""

    model_config = ConfigDict(extra="forbid")

    facility: str
    bid: Decimal
    buyers_premium: Decimal
    sales_tax: Decimal
    hauling_cost: Decimal
    disposal_cost: Decimal
    labor_cost: Decimal
    total_cost: Decimal

    expected_revenue: Decimal
    net_profit: Decimal
    roi_percent: Decimal
    #: Highest bid at which this unit still breaks even. Take this number to
    #: the auction and stop there.
    max_profitable_bid: Decimal

    capital_required: Decimal = Field(
        description="Cash you must have on hand, deposit included."
    )
    clearout_feasible: bool
    warnings: list[str] = Field(default_factory=list)


def evaluate_storage_unit(
    unit: StorageUnitAuction,
    *,
    cost_per_mile: Decimal = Decimal("0.65"),
    hourly_value_of_time: Decimal = Decimal("25"),
    available_hours_before_deadline: Decimal | None = None,
) -> StorageUnitEstimate:
    """Price a unit end to end and find the walk-away bid.

    ``cost_per_mile`` should reflect a loaded truck, not a car - fuel plus
    the wear you are really spending.

    ``available_hours_before_deadline`` is how many hours you can actually put
    in before the clear-out window closes. Leave it ``None`` if you have not
    checked; the result will say so rather than assume you are free.
    """
    warnings: list[str] = []

    premium = _money(unit.bid * unit.buyers_premium_rate)
    taxable = unit.bid + premium
    tax = _money(taxable * unit.sales_tax_rate)
    hauling = _money(unit.round_trip_miles * Decimal(unit.loads_required) * cost_per_mile)
    labor = _money(unit.labor_hours * hourly_value_of_time)

    total_cost = _money(unit.bid + premium + tax + hauling + unit.disposal_cost + labor)
    revenue = _money(unit.estimated_contents_value * unit.sellable_fraction)
    net = _money(revenue - total_cost)

    roi = _money(net / total_cost * Decimal("100")) if total_cost > 0 else Decimal("0.00")

    # Walk-away bid: the bid at which net profit hits zero, holding every
    # other cost fixed. Premium and tax scale with the bid, so solve for it:
    #   revenue = bid*(1 + p)*(1 + t) + fixed   ->   bid = (revenue - fixed)/k
    fixed = hauling + unit.disposal_cost + labor
    scale = (Decimal("1") + unit.buyers_premium_rate) * (
        Decimal("1") + unit.sales_tax_rate
    )
    max_bid = _money(max((revenue - fixed) / scale, Decimal("0")))

    capital = _money(total_cost + unit.cleaning_deposit)

    feasible = True
    if available_hours_before_deadline is not None:
        feasible = unit.labor_hours <= available_hours_before_deadline
        if not feasible:
            warnings.append(
                f"Clear-out needs ~{unit.labor_hours}h but only "
                f"{available_hours_before_deadline}h are available before the "
                f"{unit.clearout_deadline_hours}h deadline. Missing it forfeits "
                f"the ${unit.cleaning_deposit} deposit."
            )
    else:
        warnings.append(
            "Clear-out feasibility unchecked - confirm you can empty this "
            f"within {unit.clearout_deadline_hours}h before bidding."
        )

    if unit.sellable_fraction >= Decimal("0.5"):
        warnings.append(
            f"sellable_fraction of {unit.sellable_fraction:.0%} is optimistic "
            "for a sight-unseen unit; most rooms are mostly dump weight."
        )
    if unit.disposal_cost == 0:
        warnings.append(
            "disposal_cost is 0. Every unit has trash, and dump fees are where "
            "storage-auction margins usually die."
        )
    if max_bid <= 0:
        # Hauling, disposal, and labour alone exceed the expected revenue, so
        # no bid clears - this unit loses money even if it is handed to you
        # free. Worth saying outright rather than as "your bid is too high".
        warnings.append(
            "UNPROFITABLE AT ANY BID - hauling, disposal and labour alone "
            f"(${fixed}) exceed expected revenue (${revenue}). Do not bid."
        )
    elif net < 0:
        warnings.append(f"NET LOSS at this bid. Walk away above ${max_bid}.")
    elif unit.bid > max_bid:
        warnings.append(f"Bid exceeds the break-even point of ${max_bid}.")

    return StorageUnitEstimate(
        facility=unit.facility,
        bid=_money(unit.bid),
        buyers_premium=premium,
        sales_tax=tax,
        hauling_cost=hauling,
        disposal_cost=_money(unit.disposal_cost),
        labor_cost=labor,
        total_cost=total_cost,
        expected_revenue=revenue,
        net_profit=net,
        roi_percent=roi,
        max_profitable_bid=max_bid,
        capital_required=capital,
        clearout_feasible=feasible,
        warnings=warnings,
    )


def format_storage_estimate(estimate: StorageUnitEstimate) -> str:
    """Render a unit evaluation as plain text."""
    lines = [
        f"Storage unit: {estimate.facility}",
        "",
        f"  bid                 ${estimate.bid:>10}",
        f"  buyer's premium     ${estimate.buyers_premium:>10}",
        f"  sales tax           ${estimate.sales_tax:>10}",
        f"  hauling             ${estimate.hauling_cost:>10}",
        f"  disposal            ${estimate.disposal_cost:>10}",
        f"  your time           ${estimate.labor_cost:>10}",
        f"  {'-' * 30}",
        f"  TOTAL COST          ${estimate.total_cost:>10}",
        f"  expected revenue    ${estimate.expected_revenue:>10}",
        f"  NET PROFIT          ${estimate.net_profit:>10}   ({estimate.roi_percent}% ROI)",
        "",
        f"  WALK AWAY ABOVE     ${estimate.max_profitable_bid:>10}",
        f"  cash needed         ${estimate.capital_required:>10}  (incl. deposit)",
        "",
    ]
    if estimate.warnings:
        lines.append("  Warnings:")
        lines.extend(f"    - {w}" for w in estimate.warnings)
    return "\n".join(lines)

"""Pydantic v2 models shared across the FlipScout backend.

Money is modelled with :class:`decimal.Decimal` end to end. Binary floating
point is never used for prices, fees, or profit: ``0.1 + 0.2 != 0.3`` in
IEEE-754, and a resale tool that is a cent off is a resale tool nobody trusts.

Serialization note: Pydantic v2 emits ``Decimal`` as a JSON *string*
(``"12.34"``). That is deliberate and preserved here — it round-trips exactly,
whereas a JSON number would be re-parsed as a float by most clients.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------- #
# Marketplace-neutral listing
# --------------------------------------------------------------------------- #

MarketplaceSource = Literal["ebay", "facebook"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NormalizedListing(BaseModel):
    """A listing from any marketplace, flattened to one shape.

    Only ``source`` and ``captured_at`` are guaranteed. Every other field is
    optional because upstream payloads routinely omit images, seller blocks,
    shipping options, or location data — and a normalizer that raises on a
    missing thumbnail is useless in production. Callers must treat ``None`` as
    "the marketplace did not tell us", not as zero.

    ``raw`` keeps the original upstream object for debugging. It is sanitized
    (see ``services.normalization.sanitize_raw``) so no credential or
    ``Authorization`` header can ride along into a response or a log line.
    """

    model_config = ConfigDict(extra="forbid")

    source: MarketplaceSource
    source_item_id: str | None = None
    title: str | None = None
    url: str | None = None
    image_url: str | None = None

    price_value: Decimal | None = None
    price_currency: str | None = None
    shipping_cost: Decimal | None = None

    condition: str | None = None

    seller_username: str | None = None
    seller_feedback_score: int | None = None

    location_text: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    category_id: str | None = None
    category_name: str | None = None

    listing_type: str | None = None
    item_end_time: datetime | None = None

    captured_at: datetime = Field(default_factory=_utcnow)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_acquisition_cost(self) -> Decimal | None:
        """Price plus shipping, when both are known."""
        if self.price_value is None:
            return None
        return self.price_value + (self.shipping_cost or Decimal("0"))


class EbayItemCondition(StrEnum):
    """Condition values accepted by the eBay Browse ``conditions`` filter.

    These are eBay's own documented enum names. The API is given exactly these
    strings — nothing here is invented, and an unlisted value is rejected by
    FastAPI with a 422 before any upstream request is made.
    """

    NEW = "NEW"
    LIKE_NEW = "LIKE_NEW"
    NEW_OTHER = "NEW_OTHER"
    NEW_WITH_DEFECTS = "NEW_WITH_DEFECTS"
    MANUFACTURER_REFURBISHED = "MANUFACTURER_REFURBISHED"
    CERTIFIED_REFURBISHED = "CERTIFIED_REFURBISHED"
    EXCELLENT_REFURBISHED = "EXCELLENT_REFURBISHED"
    VERY_GOOD_REFURBISHED = "VERY_GOOD_REFURBISHED"
    GOOD_REFURBISHED = "GOOD_REFURBISHED"
    SELLER_REFURBISHED = "SELLER_REFURBISHED"
    USED_EXCELLENT = "USED_EXCELLENT"
    USED_VERY_GOOD = "USED_VERY_GOOD"
    USED_GOOD = "USED_GOOD"
    USED_ACCEPTABLE = "USED_ACCEPTABLE"
    FOR_PARTS_OR_NOT_WORKING = "FOR_PARTS_OR_NOT_WORKING"


class ListingSearchResponse(BaseModel):
    """Envelope returned by ``GET /api/ebay/search``."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(
        default=0,
        ge=0,
        description="Total matches eBay reports for the query, not the page size.",
    )
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    listings: list[NormalizedListing] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Profitability
# --------------------------------------------------------------------------- #

# eBay's managed-payments final value fee already bundles payment processing,
# so payment_fee_rate defaults to 0 to avoid double-counting it. Set it
# explicitly when modelling a marketplace that bills processing separately.
DEFAULT_MARKETPLACE_FEE_RATE = Decimal("0.1325")
DEFAULT_PAYMENT_FEE_RATE = Decimal("0")

_MONEY = {"ge": 0, "max_digits": 16, "decimal_places": 4}


class ProfitAssumptions(BaseModel):
    """Inputs to a profit estimate.

    Every figure is an *assumption supplied by the caller*. This model performs
    arithmetic on the numbers it is given; it does not source, validate, or
    vouch for the resale price. See ``services.profitability`` for why that
    distinction matters.
    """

    model_config = ConfigDict(extra="forbid")

    resale_price: Decimal = Field(
        ...,
        description="Expected sale price. Should come from sold/completed "
        "comparables, not from a current asking price.",
        **_MONEY,
    )
    purchase_price: Decimal = Field(
        ...,
        gt=0,
        max_digits=16,
        decimal_places=4,
        description="What you pay for the item. Must be positive: it is the "
        "denominator of every multiple and ROI figure.",
    )

    marketplace_fee_rate: Decimal = Field(
        default=DEFAULT_MARKETPLACE_FEE_RATE,
        ge=0,
        le=1,
        decimal_places=6,
        description="Fraction of resale price taken by the marketplace, e.g. "
        "0.1325 for a 13.25% final value fee.",
    )
    payment_fee_rate: Decimal = Field(
        default=DEFAULT_PAYMENT_FEE_RATE,
        ge=0,
        le=1,
        decimal_places=6,
        description="Fraction of resale price taken by payment processing. "
        "Defaults to 0 because eBay bundles this into the final value fee.",
    )

    shipping_cost: Decimal = Field(default=Decimal("0"), **_MONEY)
    taxes: Decimal = Field(default=Decimal("0"), **_MONEY)
    fuel_cost: Decimal = Field(default=Decimal("0"), **_MONEY)
    repair_cost: Decimal = Field(default=Decimal("0"), **_MONEY)
    cleaning_cost: Decimal = Field(default=Decimal("0"), **_MONEY)
    packaging_cost: Decimal = Field(default=Decimal("0"), **_MONEY)
    other_costs: Decimal = Field(default=Decimal("0"), **_MONEY)

    @model_validator(mode="after")
    def _fee_rates_are_sane(self) -> ProfitAssumptions:
        """Reject a combined fee rate above 100% of the sale price."""
        if self.marketplace_fee_rate + self.payment_fee_rate > 1:
            raise ValueError(
                "marketplace_fee_rate + payment_fee_rate must not exceed 1.0 "
                "(100% of the resale price)"
            )
        return self


class ProfitEstimate(BaseModel):
    """Transparent, fully itemized output of a profit calculation.

    Every component is returned so a caller can re-derive ``net_profit`` by
    hand. Nothing is hidden behind a single score.
    """

    model_config = ConfigDict(extra="forbid")

    resale_price: Decimal
    purchase_price: Decimal

    gross_profit: Decimal = Field(description="resale_price - purchase_price")
    total_selling_fees: Decimal = Field(
        description="resale_price * (marketplace_fee_rate + payment_fee_rate)"
    )
    total_other_costs: Decimal = Field(
        description="shipping + taxes + fuel + repair + cleaning + packaging + other"
    )
    net_profit: Decimal = Field(
        description="resale_price - purchase_price - total_selling_fees - total_other_costs"
    )

    gross_multiple: Decimal = Field(description="resale_price / purchase_price")
    gross_roi_percent: Decimal = Field(
        description="gross_profit / purchase_price * 100. A 5x multiple is 400% "
        "here, not 500%."
    )
    net_roi_percent: Decimal = Field(
        description="net_profit / (purchase_price + total_selling_fees + "
        "total_other_costs) * 100"
    )

    qualifies_for_500_percent_resale_multiple: bool = Field(
        description="True when gross_multiple >= 5, i.e. resale is at least 5x "
        "the purchase price. This is a RESALE MULTIPLE test, deliberately "
        "distinct from gross_roi_percent >= 500."
    )

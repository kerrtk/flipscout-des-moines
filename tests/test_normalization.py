"""Normalization must never raise on a real-world-shaped payload."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.normalization import (
    NormalizationError,
    normalize_ebay_item,
    normalize_ebay_search_response,
    sanitize_raw,
    to_decimal,
)

# --------------------------------------------------------------------------- #
# 3. Successful normalization
# --------------------------------------------------------------------------- #


def test_normalizes_a_complete_item(sample_ebay_item) -> None:
    listing = normalize_ebay_item(sample_ebay_item)

    assert listing.source == "ebay"
    assert listing.source_item_id == "v1|123456789012|0"
    assert listing.title == "Vintage Pyrex Mixing Bowl Set"
    assert listing.url == "https://www.ebay.com/itm/123456789012"
    assert listing.image_url == "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"
    assert listing.price_value == Decimal("24.99")
    assert listing.price_currency == "USD"
    assert listing.shipping_cost == Decimal("8.45")
    assert listing.condition == "Used"
    assert listing.seller_username == "desmoines_finds"
    assert listing.seller_feedback_score == 1423
    assert listing.location_text == "Des Moines, IA, US"
    assert listing.postal_code == "50309"
    assert listing.category_id == "20641"
    assert listing.category_name == "Bowls"
    assert listing.listing_type == "FIXED_PRICE"
    assert listing.item_end_time == datetime(2026, 9, 30, 17, 45, tzinfo=UTC)
    assert listing.captured_at.tzinfo is not None


def test_monetary_values_are_exact_decimals(sample_ebay_item) -> None:
    """Prices must be Decimal, never float."""
    listing = normalize_ebay_item(sample_ebay_item)
    assert isinstance(listing.price_value, Decimal)
    assert isinstance(listing.shipping_cost, Decimal)
    assert listing.total_acquisition_cost == Decimal("33.44")


def test_raw_payload_is_preserved_for_debugging(sample_ebay_item) -> None:
    listing = normalize_ebay_item(sample_ebay_item)
    assert listing.raw["itemId"] == sample_ebay_item["itemId"]
    assert listing.raw["seller"]["feedbackPercentage"] == "99.6"


def test_raw_is_a_copy_not_a_reference(sample_ebay_item) -> None:
    """Mutating the caller's dict afterwards must not alter a stored listing."""
    listing = normalize_ebay_item(sample_ebay_item)
    sample_ebay_item["title"] = "MUTATED"
    sample_ebay_item["seller"]["username"] = "MUTATED"
    assert listing.raw["title"] == "Vintage Pyrex Mixing Bowl Set"
    assert listing.raw["seller"]["username"] == "desmoines_finds"


def test_buying_options_are_sorted_for_stable_output() -> None:
    a = normalize_ebay_item({"buyingOptions": ["FIXED_PRICE", "AUCTION"]})
    b = normalize_ebay_item({"buyingOptions": ["AUCTION", "FIXED_PRICE"]})
    assert a.listing_type == b.listing_type == "AUCTION|FIXED_PRICE"


# --------------------------------------------------------------------------- #
# 4. Missing optional fields
# --------------------------------------------------------------------------- #


def test_empty_payload_normalizes_without_raising() -> None:
    """The degenerate case: an object with nothing in it."""
    listing = normalize_ebay_item({})

    assert listing.source == "ebay"
    assert listing.source_item_id is None
    assert listing.title is None
    assert listing.price_value is None
    assert listing.shipping_cost is None
    assert listing.image_url is None
    assert listing.seller_username is None
    assert listing.location_text is None
    assert listing.listing_type is None
    assert listing.item_end_time is None
    assert listing.raw == {}


def test_missing_image_block_falls_back_to_thumbnail() -> None:
    listing = normalize_ebay_item(
        {"thumbnailImages": [{"imageUrl": "https://example.com/thumb.jpg"}]}
    )
    assert listing.image_url == "https://example.com/thumb.jpg"


def test_missing_seller_information_is_tolerated() -> None:
    listing = normalize_ebay_item({"seller": {}})
    assert listing.seller_username is None
    assert listing.seller_feedback_score is None


def test_seller_block_of_the_wrong_type_is_tolerated() -> None:
    """Defensive: upstream sends a string where an object was documented."""
    listing = normalize_ebay_item({"seller": "not-an-object", "categories": "nope"})
    assert listing.seller_username is None
    assert listing.category_id is None


def test_unexpected_optional_fields_are_ignored_but_kept_in_raw() -> None:
    listing = normalize_ebay_item(
        {"itemId": "v1|1|0", "brandNewFieldEbayAddedYesterday": {"nested": [1, 2]}}
    )
    assert listing.source_item_id == "v1|1|0"
    assert listing.raw["brandNewFieldEbayAddedYesterday"] == {"nested": [1, 2]}


def test_affiliate_url_is_used_when_plain_url_missing() -> None:
    listing = normalize_ebay_item({"itemAffiliateWebUrl": "https://ebay.com/itm/9?aff=1"})
    assert listing.url == "https://ebay.com/itm/9?aff=1"


def test_partial_location_produces_partial_text() -> None:
    listing = normalize_ebay_item({"itemLocation": {"country": "US"}})
    assert listing.location_text == "US"
    assert listing.postal_code is None


def test_coordinates_are_parsed_when_present() -> None:
    listing = normalize_ebay_item(
        {"itemLocation": {"latitude": "41.5868", "longitude": -93.625}}
    )
    assert listing.latitude == pytest.approx(41.5868)
    assert listing.longitude == pytest.approx(-93.625)


def test_non_object_input_raises_normalization_error() -> None:
    with pytest.raises(NormalizationError):
        normalize_ebay_item(["not", "an", "object"])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 5. Invalid or missing prices
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_value",
    ["", "   ", "abc", "$24.99", None, {}, [], True, "NaN", "Infinity", "-5.00"],
)
def test_invalid_price_becomes_none(bad_value) -> None:
    """A price we cannot trust is None, never a guess and never a crash."""
    listing = normalize_ebay_item({"price": {"value": bad_value, "currency": "USD"}})
    assert listing.price_value is None
    assert listing.price_currency == "USD"


def test_missing_price_block_entirely() -> None:
    listing = normalize_ebay_item({"itemId": "v1|1|0"})
    assert listing.price_value is None
    assert listing.price_currency is None
    assert listing.total_acquisition_cost is None


def test_numeric_price_types_are_accepted_without_float_error() -> None:
    """A JSON number must not inherit binary floating-point drift."""
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(24) == Decimal("24")
    assert to_decimal("1,299.00") == Decimal("1299.00")


def test_zero_price_is_preserved_not_treated_as_missing() -> None:
    listing = normalize_ebay_item({"price": {"value": "0.00", "currency": "USD"}})
    assert listing.price_value == Decimal("0.00")


def test_invalid_feedback_score_becomes_none() -> None:
    listing = normalize_ebay_item({"seller": {"feedbackScore": "many"}})
    assert listing.seller_feedback_score is None


def test_string_feedback_score_is_coerced() -> None:
    listing = normalize_ebay_item({"seller": {"feedbackScore": "1423"}})
    assert listing.seller_feedback_score == 1423


def test_unparsable_end_date_becomes_none() -> None:
    listing = normalize_ebay_item({"itemEndDate": "next tuesday"})
    assert listing.item_end_time is None


# --------------------------------------------------------------------------- #
# 6. Shipping-cost normalization
# --------------------------------------------------------------------------- #


def test_free_shipping_is_zero_not_none() -> None:
    """Distinguishing "ships free" from "unknown" is a margin-critical detail."""
    listing = normalize_ebay_item(
        {"shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "USD"}}]}
    )
    assert listing.shipping_cost == Decimal("0.00")
    assert listing.shipping_cost is not None


def test_missing_shipping_options_yields_none() -> None:
    assert normalize_ebay_item({}).shipping_cost is None


def test_empty_shipping_options_list_yields_none() -> None:
    assert normalize_ebay_item({"shippingOptions": []}).shipping_cost is None


def test_calculated_shipping_without_amount_yields_none() -> None:
    """Calculated shipping has no quotable cost until a buyer ZIP is known."""
    listing = normalize_ebay_item(
        {"shippingOptions": [{"shippingCostType": "CALCULATED", "shippingCost": None}]}
    )
    assert listing.shipping_cost is None


def test_first_parsable_shipping_option_wins() -> None:
    listing = normalize_ebay_item(
        {
            "shippingOptions": [
                {"shippingCostType": "CALCULATED"},
                {"shippingCost": {"value": "12.50", "currency": "USD"}},
                {"shippingCost": {"value": "30.00", "currency": "USD"}},
            ]
        }
    )
    assert listing.shipping_cost == Decimal("12.50")


def test_shipping_cost_adds_into_acquisition_cost() -> None:
    listing = normalize_ebay_item(
        {
            "price": {"value": "10.00", "currency": "USD"},
            "shippingOptions": [{"shippingCost": {"value": "5.55", "currency": "USD"}}],
        }
    )
    assert listing.total_acquisition_cost == Decimal("15.55")


def test_unknown_shipping_does_not_inflate_acquisition_cost() -> None:
    """Unknown shipping is treated as 0 for the sum, and the None is visible."""
    listing = normalize_ebay_item({"price": {"value": "10.00", "currency": "USD"}})
    assert listing.shipping_cost is None
    assert listing.total_acquisition_cost == Decimal("10.00")


# --------------------------------------------------------------------------- #
# Credential safety in `raw`
# --------------------------------------------------------------------------- #


def test_sanitize_raw_redacts_credential_like_keys() -> None:
    cleaned = sanitize_raw(
        {
            "Authorization": "Bearer super-secret",
            "nested": {"client_secret": "hunter2", "apiKey": "abc", "title": "ok"},
            "list": [{"access_token": "tok"}],
        }
    )
    assert cleaned["Authorization"] == "[redacted]"
    assert cleaned["nested"]["client_secret"] == "[redacted]"
    assert cleaned["nested"]["apiKey"] == "[redacted]"
    assert cleaned["nested"]["title"] == "ok"
    assert cleaned["list"][0]["access_token"] == "[redacted]"


def test_normalized_raw_never_carries_an_authorization_header() -> None:
    listing = normalize_ebay_item(
        {"itemId": "v1|1|0", "debugHeaders": {"Authorization": "Bearer leak"}}
    )
    assert "leak" not in str(listing.raw)
    assert listing.raw["debugHeaders"]["Authorization"] == "[redacted]"


# --------------------------------------------------------------------------- #
# Whole-response normalization
# --------------------------------------------------------------------------- #


def test_search_response_normalization_skips_unusable_rows(sample_ebay_item) -> None:
    """One malformed row must not cost the caller the rest of the page."""
    listings = normalize_ebay_search_response(
        {"itemSummaries": [sample_ebay_item, "not-an-object", {}, None]}
    )
    assert len(listings) == 2
    assert listings[0].source_item_id == "v1|123456789012|0"


def test_search_response_without_items_returns_empty_list() -> None:
    assert normalize_ebay_search_response({"total": 0}) == []
    assert normalize_ebay_search_response({}) == []

"""Marketplace payload -> :class:`NormalizedListing`.

Design rule: **never raise on a malformed listing.** eBay's Browse responses
are heterogeneous — auctions have no shipping option, some items have no
image, sellers occasionally have no feedback score, and calculated shipping
returns a cost of ``null``. A search that returns 50 items should yield 50
normalized rows, with unknown values as ``None``, rather than a 500.

The only hard requirement is that the input is a JSON object.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models import NormalizedListing

# Keys that must never survive into ``raw``. Matched case-insensitively as
# substrings so ``X-EBAY-C-Authorization`` and ``clientSecret`` are both caught.
_REDACTED_KEY_FRAGMENTS: tuple[str, ...] = (
    "authorization",
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "client_secret",
    "clientsecret",
    "password",
    "api_key",
    "apikey",
    "credential",
)

_REDACTION_PLACEHOLDER = "[redacted]"


class NormalizationError(ValueError):
    """The payload was not an object and could not be normalized at all."""


# --------------------------------------------------------------------------- #
# Coercion helpers - each returns None rather than raising
# --------------------------------------------------------------------------- #


def to_decimal(value: Any) -> Decimal | None:
    """Coerce an upstream monetary value to ``Decimal``, or ``None``.

    Returns ``None`` for missing, empty, non-numeric, negative, and non-finite
    (``NaN``/``Infinity``) inputs. eBay sends money as JSON *strings*
    (``"12.34"``), which is why the string branch is the common path — and why
    a float is converted via ``str()`` to avoid inheriting binary rounding
    error such as ``Decimal(0.1) == 0.1000000000000000055511151231257827``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        candidate = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            candidate = Decimal(text)
        except InvalidOperation:
            return None
    else:
        return None

    if not candidate.is_finite():
        return None
    if candidate < 0:
        # A negative price or shipping cost is nonsense; treat it as unknown.
        return None
    return candidate


def to_int(value: Any) -> int | None:
    """Coerce to ``int``, tolerating numeric strings; ``None`` on failure."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def to_float(value: Any) -> float | None:
    """Coerce a coordinate to ``float``; ``None`` on failure."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            result = float(text)
        except ValueError:
            return None
    else:
        return None
    # Reject NaN/inf, which JSON encoders cannot represent.
    if not math.isfinite(result):
        return None
    return result


def to_text(value: Any) -> str | None:
    """Return a stripped non-empty string, or ``None``."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def to_datetime(value: Any) -> datetime | None:
    """Parse an eBay ISO-8601 timestamp (``...Z``) into an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = to_text(value)
    if text is None:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` if it is a mapping, else an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Iterable[Any]:
    """Return ``value`` if it is a list/tuple, else an empty tuple."""
    return value if isinstance(value, (list, tuple)) else ()


# --------------------------------------------------------------------------- #
# raw sanitization
# --------------------------------------------------------------------------- #


def sanitize_raw(payload: Any) -> Any:
    """Deep-copy a payload with any credential-looking key redacted.

    ``raw`` exists so a developer can see exactly what eBay sent. It must never
    become a channel through which a token or secret reaches a response body,
    a log aggregator, or a browser devtools panel.
    """
    if isinstance(payload, Mapping):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            key_str = str(key)
            if any(frag in key_str.lower() for frag in _REDACTED_KEY_FRAGMENTS):
                cleaned[key_str] = _REDACTION_PLACEHOLDER
            else:
                cleaned[key_str] = sanitize_raw(value)
        return cleaned
    if isinstance(payload, (list, tuple)):
        return [sanitize_raw(item) for item in payload]
    return copy.deepcopy(payload)


# --------------------------------------------------------------------------- #
# Field extraction
# --------------------------------------------------------------------------- #


def _extract_image_url(raw_item: Mapping[str, Any]) -> str | None:
    """Prefer the primary image, then a thumbnail, then any additional image."""
    primary = to_text(_mapping(raw_item.get("image")).get("imageUrl"))
    if primary:
        return primary
    for collection_key in ("thumbnailImages", "additionalImages"):
        for entry in _sequence(raw_item.get(collection_key)):
            url = to_text(_mapping(entry).get("imageUrl"))
            if url:
                return url
    return None


def _extract_shipping_cost(raw_item: Mapping[str, Any]) -> Decimal | None:
    """Return the first parsable shipping cost across all shipping options.

    Free shipping is a real ``Decimal("0.00")``, not ``None`` — the difference
    between "ships free" and "we don't know the shipping cost" is exactly the
    kind of thing that silently destroys a margin estimate. Calculated shipping
    with no quoted amount stays ``None``.
    """
    for option in _sequence(raw_item.get("shippingOptions")):
        cost = to_decimal(_mapping(_mapping(option).get("shippingCost")).get("value"))
        if cost is not None:
            return cost
    return None


def _extract_location(
    raw_item: Mapping[str, Any],
) -> tuple[str | None, str | None, float | None, float | None]:
    """Return ``(location_text, postal_code, latitude, longitude)``."""
    location = _mapping(raw_item.get("itemLocation"))
    parts = [
        to_text(location.get("city")),
        to_text(location.get("stateOrProvince")),
        to_text(location.get("country")),
    ]
    location_text = ", ".join(part for part in parts if part) or None
    return (
        location_text,
        to_text(location.get("postalCode")),
        to_float(location.get("latitude")),
        to_float(location.get("longitude")),
    )


def _extract_category(
    raw_item: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Return ``(category_id, category_name)`` from the first category entry."""
    for entry in _sequence(raw_item.get("categories")):
        category = _mapping(entry)
        category_id = to_text(category.get("categoryId"))
        category_name = to_text(category.get("categoryName"))
        if category_id or category_name:
            return category_id, category_name
    # Some Browse payloads carry a bare categoryId at the top level instead.
    return to_text(raw_item.get("categoryId")), None


def _extract_listing_type(raw_item: Mapping[str, Any]) -> str | None:
    """Join eBay's ``buyingOptions`` list into a stable, sorted string.

    Sorted so ``["FIXED_PRICE", "AUCTION"]`` and ``["AUCTION", "FIXED_PRICE"]``
    normalize identically, which keeps downstream grouping deterministic.
    """
    options = [to_text(option) for option in _sequence(raw_item.get("buyingOptions"))]
    present = sorted({option for option in options if option})
    return "|".join(present) or None


def _extract_url(raw_item: Mapping[str, Any]) -> str | None:
    """Prefer the plain web URL; fall back to the affiliate variant."""
    return to_text(raw_item.get("itemWebUrl")) or to_text(
        raw_item.get("itemAffiliateWebUrl")
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def normalize_ebay_item(raw_item: dict) -> NormalizedListing:
    """Convert one eBay Browse ``itemSummary`` into a ``NormalizedListing``.

    Unknown or unparsable values become ``None``; unexpected extra keys are
    ignored by the field extractors but preserved in ``raw``.

    Raises:
        NormalizationError: only if ``raw_item`` is not a JSON object.
    """
    if not isinstance(raw_item, Mapping):
        raise NormalizationError(
            f"Expected a JSON object for an eBay item, got {type(raw_item).__name__}."
        )

    price = _mapping(raw_item.get("price"))
    seller = _mapping(raw_item.get("seller"))
    location_text, postal_code, latitude, longitude = _extract_location(raw_item)
    category_id, category_name = _extract_category(raw_item)

    return NormalizedListing(
        source="ebay",
        source_item_id=to_text(raw_item.get("itemId"))
        or to_text(raw_item.get("legacyItemId")),
        title=to_text(raw_item.get("title")),
        url=_extract_url(raw_item),
        image_url=_extract_image_url(raw_item),
        price_value=to_decimal(price.get("value")),
        price_currency=to_text(price.get("currency")),
        shipping_cost=_extract_shipping_cost(raw_item),
        condition=to_text(raw_item.get("condition")),
        seller_username=to_text(seller.get("username")),
        seller_feedback_score=to_int(seller.get("feedbackScore")),
        location_text=location_text,
        postal_code=postal_code,
        latitude=latitude,
        longitude=longitude,
        category_id=category_id,
        category_name=category_name,
        listing_type=_extract_listing_type(raw_item),
        item_end_time=to_datetime(raw_item.get("itemEndDate")),
        raw=sanitize_raw(raw_item),
    )


def normalize_ebay_search_response(payload: Mapping[str, Any]) -> list[NormalizedListing]:
    """Normalize every ``itemSummaries`` entry, skipping unusable rows.

    A single malformed item never costs the caller the whole page.
    """
    listings: list[NormalizedListing] = []
    for entry in _sequence(_mapping(payload).get("itemSummaries")):
        try:
            listings.append(normalize_ebay_item(entry))
        except (NormalizationError, ValueError):
            # Log-and-skip: one bad row must not fail an otherwise good page.
            continue
    return listings

"""Shared fixtures.

Every test in this suite is offline. No test opens a socket to eBay: the OAuth
and Browse round-trips are served by ``httpx.MockTransport``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from app.config import Settings, get_settings
from app.services.ebay_client import EbayClient

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate every test from the developer's real environment.

    Guards against a machine with real EBAY_* variables set silently changing
    test behaviour — or worse, a test accidentally reaching production.
    """
    for name in (
        "EBAY_CLIENT_ID",
        "EBAY_CLIENT_SECRET",
        "EBAY_API_BASE",
        "EBAY_MARKETPLACE_ID",
        "REQUEST_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """Settings with obviously fake credentials."""
    return Settings(
        ebay_client_id="test-client-id",
        ebay_client_secret="test-client-secret",
        ebay_marketplace_id="EBAY_US",
        request_timeout_seconds=5,
    )


@pytest.fixture
def make_client(
    settings: Settings,
) -> Callable[[Callable[[httpx.Request], httpx.Response]], EbayClient]:
    """Build an ``EbayClient`` whose transport is a caller-supplied handler."""

    def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> EbayClient:
        transport = httpx.MockTransport(handler)
        return EbayClient(settings, client=httpx.Client(transport=transport))

    return _factory


def token_response(
    *, token: str = "test-access-token", expires_in: int = 7200
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": token,
            "expires_in": expires_in,
            "token_type": "Application Access Token",
        },
    )


@pytest.fixture
def sample_ebay_item() -> dict[str, Any]:
    """A complete, realistic Browse ``itemSummary``."""
    return {
        "itemId": "v1|123456789012|0",
        "title": "Vintage Pyrex Mixing Bowl Set",
        "itemWebUrl": "https://www.ebay.com/itm/123456789012",
        "image": {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"},
        "thumbnailImages": [
            {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l225.jpg"}
        ],
        "price": {"value": "24.99", "currency": "USD"},
        "shippingOptions": [
            {
                "shippingCostType": "FIXED",
                "shippingCost": {"value": "8.45", "currency": "USD"},
            }
        ],
        "condition": "Used",
        "conditionId": "3000",
        "seller": {
            "username": "desmoines_finds",
            "feedbackScore": 1423,
            "feedbackPercentage": "99.6",
        },
        "itemLocation": {
            "city": "Des Moines",
            "stateOrProvince": "IA",
            "postalCode": "50309",
            "country": "US",
        },
        "categories": [
            {"categoryId": "20641", "categoryName": "Bowls"},
            {"categoryId": "870", "categoryName": "Pottery & Glass"},
        ],
        "buyingOptions": ["FIXED_PRICE"],
        "itemEndDate": "2026-09-30T17:45:00.000Z",
    }

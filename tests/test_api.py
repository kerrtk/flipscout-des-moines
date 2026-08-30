"""Endpoint behaviour and HTTP status mapping. No live eBay calls."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import MissingConfigurationError, Settings
from app.main import app, get_app_settings, get_ebay_client
from app.services.ebay_client import (
    EbayApiError,
    EbayAuthError,
    EbayClient,
    EbayRateLimitError,
    EbayResponseError,
    EbayTimeoutError,
)
from tests.conftest import token_response


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def use_ebay_client(handler) -> EbayClient:
    """Point the app at an EbayClient backed by a mock transport."""
    settings = Settings(
        ebay_client_id="test-id",
        ebay_client_secret="test-secret",
    )
    ebay_client = EbayClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    app.dependency_overrides[get_ebay_client] = lambda: ebay_client
    return ebay_client


def raising_client(exception: Exception) -> None:
    """Override the dependency with a client whose search always raises."""

    class _Failing:
        def search(self, **_kwargs):
            raise exception

    failing = _Failing()
    app.dependency_overrides[get_ebay_client] = lambda: failing


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #


def test_health_reports_ok_without_credentials(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ebay_credentials_configured"] is False
    assert body["ebay_api_base"] == "https://api.ebay.com"
    assert body["ebay_marketplace_id"] == "EBAY_US"


def test_health_never_echoes_the_secret(client: TestClient) -> None:
    app.dependency_overrides[get_app_settings] = lambda: Settings(
        ebay_client_id="visible-id",
        ebay_client_secret="TOP-SECRET-VALUE",
    )
    response = client.get("/health")
    assert response.status_code == 200
    assert "TOP-SECRET-VALUE" not in response.text
    assert response.json()["ebay_credentials_configured"] is True


def test_openapi_schema_is_served(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "FlipScout Des Moines API"
    for path in (
        "/health",
        "/api/ebay/search",
        "/api/normalize/ebay",
        "/api/profit-estimate",
    ):
        assert path in schema["paths"]


# --------------------------------------------------------------------------- #
# /api/ebay/search
# --------------------------------------------------------------------------- #


def test_search_returns_normalized_listings(client: TestClient, sample_ebay_item) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={
                "total": 137,
                "offset": 0,
                "limit": 2,
                "itemSummaries": [sample_ebay_item],
            },
        )

    use_ebay_client(handler)
    response = client.get("/api/ebay/search", params={"q": "pyrex", "limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 137
    assert body["offset"] == 0
    assert body["limit"] == 2
    assert len(body["listings"]) == 1

    listing = body["listings"][0]
    assert listing["source"] == "ebay"
    assert listing["source_item_id"] == "v1|123456789012|0"
    # Decimal is serialized as a string so no client re-parses money as a float.
    assert listing["price_value"] == "24.99"
    assert listing["shipping_cost"] == "8.45"


def test_search_forwards_condition_and_max_price(client: TestClient) -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        captured["request"] = request
        return httpx.Response(200, json={"total": 0, "itemSummaries": []})

    use_ebay_client(handler)
    response = client.get(
        "/api/ebay/search",
        params={"q": "n64", "condition": "USED_GOOD", "max_price": 75},
    )

    assert response.status_code == 200
    filters = captured["request"].url.params["filter"]
    assert "conditions:{USED_GOOD}" in filters
    assert "price:[..75.0]" in filters
    assert "buyingOptions:{FIXED_PRICE|AUCTION}" in filters


def test_empty_result_set_returns_an_empty_list(client: TestClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(200, json={"total": 0, "offset": 0, "limit": 50})

    use_ebay_client(handler)
    body = client.get("/api/ebay/search", params={"q": "nothing"}).json()
    assert body == {"total": 0, "offset": 0, "limit": 50, "listings": []}


@pytest.mark.parametrize(
    "params",
    [
        {},  # q is required
        {"q": ""},  # q must be non-empty
        {"q": "ok", "limit": 0},
        {"q": "ok", "limit": 201},
        {"q": "ok", "offset": -1},
        {"q": "ok", "max_price": 0},
        {"q": "ok", "max_price": -1},
        {"q": "ok", "condition": "SLIGHTLY_HAUNTED"},
    ],
)
def test_invalid_query_parameters_return_422(client: TestClient, params) -> None:
    """FastAPI validates before the handler runs, so no eBay call is made."""
    raising_client(AssertionError("upstream must not be called"))
    assert client.get("/api/ebay/search", params=params).status_code == 422


# --------------------------------------------------------------------------- #
# 11. API error handling -> status codes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("exception", "expected_status", "expected_error"),
    [
        (MissingConfigurationError("EBAY_CLIENT_ID"), 503, "configuration_error"),
        (EbayAuthError("rejected", status_code=401), 502, "upstream_auth_error"),
        (EbayApiError("boom", status_code=500), 502, "upstream_error"),
        (EbayResponseError("bad json"), 502, "upstream_malformed_response"),
        (EbayRateLimitError("slow down", status_code=429), 429, "rate_limited"),
        (EbayTimeoutError("timed out"), 504, "upstream_timeout"),
    ],
)
def test_upstream_failures_map_to_status_codes(
    client: TestClient, exception, expected_status, expected_error
) -> None:
    raising_client(exception)
    response = client.get("/api/ebay/search", params={"q": "lego"})
    assert response.status_code == expected_status
    assert response.json()["error"] == expected_error


def test_missing_configuration_returns_503_with_the_variable_name(
    client: TestClient,
) -> None:
    """503 tells an operator exactly which variable to set — and nothing more."""
    raising_client(MissingConfigurationError("EBAY_CLIENT_SECRET"))
    response = client.get("/api/ebay/search", params={"q": "lego"})
    assert response.status_code == 503
    assert "EBAY_CLIENT_SECRET" in response.json()["detail"]


def test_upstream_error_body_never_contains_credentials(client: TestClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    use_ebay_client(handler)
    response = client.get("/api/ebay/search", params={"q": "lego"})
    assert response.status_code == 502
    assert "test-secret" not in response.text


# --------------------------------------------------------------------------- #
# /api/normalize/ebay
# --------------------------------------------------------------------------- #


def test_normalize_endpoint_returns_a_normalized_listing(
    client: TestClient, sample_ebay_item
) -> None:
    response = client.post("/api/normalize/ebay", json=sample_ebay_item)
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Vintage Pyrex Mixing Bowl Set"
    assert body["price_value"] == "24.99"
    assert body["location_text"] == "Des Moines, IA, US"


def test_normalize_endpoint_tolerates_an_empty_object(client: TestClient) -> None:
    response = client.post("/api/normalize/ebay", json={})
    assert response.status_code == 200
    assert response.json()["source"] == "ebay"
    assert response.json()["price_value"] is None


def test_normalize_endpoint_rejects_a_non_object_body(client: TestClient) -> None:
    assert client.post("/api/normalize/ebay", json=["nope"]).status_code == 422


# --------------------------------------------------------------------------- #
# /api/profit-estimate
# --------------------------------------------------------------------------- #


def test_profit_estimate_endpoint_returns_full_breakdown(client: TestClient) -> None:
    response = client.post(
        "/api/profit-estimate",
        json={
            "resale_price": "500.00",
            "purchase_price": "100.00",
            "marketplace_fee_rate": "0",
            "payment_fee_rate": "0",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["gross_multiple"] == "5.0000"
    assert body["gross_roi_percent"] == "400.00"
    assert body["qualifies_for_500_percent_resale_multiple"] is True
    assert body["net_profit"] == "400.00"


def test_profit_estimate_accepts_json_numbers_too(client: TestClient) -> None:
    response = client.post(
        "/api/profit-estimate", json={"resale_price": 100, "purchase_price": 25}
    )
    assert response.status_code == 200
    assert Decimal(response.json()["gross_multiple"]) == Decimal("4")
    assert response.json()["qualifies_for_500_percent_resale_multiple"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"resale_price": "100"},  # purchase missing
        {"purchase_price": "10"},  # resale missing
        {"resale_price": "100", "purchase_price": "0"},  # must be > 0
        {"resale_price": "100", "purchase_price": "-5"},
        {"resale_price": "-1", "purchase_price": "10"},
        {"resale_price": "100", "purchase_price": "10", "shipping_cost": "-1"},
        {"resale_price": "100", "purchase_price": "10", "marketplace_fee_rate": "2"},
        {"resale_price": "abc", "purchase_price": "10"},
    ],
)
def test_invalid_profit_payloads_return_422(client: TestClient, payload) -> None:
    assert client.post("/api/profit-estimate", json=payload).status_code == 422

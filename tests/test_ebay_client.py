"""OAuth construction, token caching, and eBay error mapping. All offline."""

from __future__ import annotations

import base64
import threading
import time
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import MissingConfigurationError, Settings
from app.services.ebay_client import (
    EbayApiError,
    EbayAuthError,
    EbayClient,
    EbayRateLimitError,
    EbayResponseError,
    EbayTimeoutError,
    InvalidSearchRequest,
)
from tests.conftest import SEARCH_URL, TOKEN_URL, token_response

# --------------------------------------------------------------------------- #
# 1. OAuth token request construction
# --------------------------------------------------------------------------- #


def test_oauth_token_request_is_built_correctly(make_client) -> None:
    """The token call must be a client-credentials grant with Basic auth."""
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return token_response()

    client = make_client(handler)
    token = client.get_access_token()

    request = captured["request"]
    assert token == "test-access-token"
    assert request.method == "POST"
    assert str(request.url) == TOKEN_URL
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"

    expected = base64.b64encode(b"test-client-id:test-client-secret").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"

    body = parse_qs(request.content.decode())
    assert body["grant_type"] == ["client_credentials"]
    assert body["scope"] == ["https://api.ebay.com/oauth/api_scope"]


def test_missing_credentials_raise_configuration_error() -> None:
    """No client id configured -> a configuration error, not a network call."""
    client = EbayClient(Settings())
    with pytest.raises(MissingConfigurationError) as exc_info:
        client.get_access_token()
    assert exc_info.value.variable == "EBAY_CLIENT_ID"


def test_missing_secret_raises_configuration_error() -> None:
    client = EbayClient(Settings(ebay_client_id="only-an-id"))
    with pytest.raises(MissingConfigurationError) as exc_info:
        client.get_access_token()
    assert exc_info.value.variable == "EBAY_CLIENT_SECRET"


def test_client_secret_never_appears_in_settings_repr(settings: Settings) -> None:
    """A settings object landing in a log line must not leak the secret."""
    rendered = f"{settings!r} {settings!s}"
    assert "test-client-secret" not in rendered
    assert settings.ebay_client_secret is not None
    assert "test-client-secret" not in repr(settings.ebay_client_secret)


# --------------------------------------------------------------------------- #
# 2. Token reuse before expiration
# --------------------------------------------------------------------------- #


def test_token_is_reused_until_it_nears_expiry(make_client) -> None:
    """A second call must reuse the cached token, not re-run the handshake."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return token_response(token=f"token-{calls['count']}")

    client = make_client(handler)

    first = client.get_access_token()
    second = client.get_access_token()
    third = client.get_access_token()

    assert first == second == third == "token-1"
    assert calls["count"] == 1, "cached token should have been reused"


def test_expired_token_triggers_a_refresh(make_client) -> None:
    """Once the cached token lapses, the next call mints a fresh one."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return token_response(token=f"token-{calls['count']}")

    client = make_client(handler)
    assert client.get_access_token() == "token-1"

    # Simulate the cached token passing its (leeway-adjusted) deadline.
    client._token.expires_at = 0.0

    assert client.get_access_token() == "token-2"
    assert calls["count"] == 2


def test_force_refresh_bypasses_the_cache(make_client) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return token_response(token=f"token-{calls['count']}")

    client = make_client(handler)
    client.get_access_token()
    assert client.get_access_token(force_refresh=True) == "token-2"
    assert calls["count"] == 2


def test_expiry_leeway_shortens_the_cache_window(make_client) -> None:
    """A 7200s token must be treated as valid for 7200 - 60 seconds."""
    client = make_client(lambda request: token_response(expires_in=7200))
    before = time.monotonic()
    client.get_access_token()
    lifetime = client._token.expires_at - before
    # The deadline is set from a monotonic reading taken *after* `before`, so
    # the observed lifetime is 7140s plus the handshake's own elapsed time.
    assert 7140 <= lifetime < 7150


def test_token_repr_does_not_leak_the_token(make_client) -> None:
    client = make_client(lambda request: token_response(token="super-secret-token"))
    client.get_access_token()
    assert "super-secret-token" not in repr(client._token)


def test_concurrent_callers_share_a_single_token_request(make_client) -> None:
    """Thread-safe refresh: N threads racing must trigger exactly one handshake."""
    calls = {"count": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            calls["count"] += 1
        return token_response(token="shared-token")

    client = make_client(handler)
    results: list[str] = []
    results_lock = threading.Lock()

    def worker() -> None:
        value = client.get_access_token()
        with results_lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["shared-token"] * 12
    assert calls["count"] == 1, "token refresh was not serialized"


# --------------------------------------------------------------------------- #
# Search parameter construction and input validation
# --------------------------------------------------------------------------- #


def test_search_requests_fixed_price_and_auction(make_client) -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        captured["request"] = request
        return httpx.Response(200, json={"total": 0, "itemSummaries": []})

    client = make_client(handler)
    client.search(keyword="pyrex bowl", limit=25, offset=50)

    request = captured["request"]
    assert request.url.path == httpx.URL(SEARCH_URL).path
    params = request.url.params
    assert params["q"] == "pyrex bowl"
    assert params["limit"] == "25"
    assert params["offset"] == "50"
    assert "buyingOptions:{FIXED_PRICE|AUCTION}" in params["filter"]
    assert request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert request.headers["Authorization"] == "Bearer test-access-token"


def test_condition_and_max_price_become_documented_filters() -> None:
    params = EbayClient.build_search_params(
        keyword="  nintendo 64  ", condition="USED_GOOD", max_price=75
    )
    assert params["q"] == "nintendo 64", "keyword should be trimmed"
    filters = params["filter"].split(",")
    assert "conditions:{USED_GOOD}" in filters
    assert "price:[..75]" in filters
    # eBay requires priceCurrency whenever the price filter is present.
    assert "priceCurrency:USD" in filters


def test_optional_filters_are_omitted_when_not_supplied() -> None:
    params = EbayClient.build_search_params(keyword="lego")
    assert params["filter"] == "buyingOptions:{FIXED_PRICE|AUCTION}"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"keyword": ""},
        {"keyword": "   "},
        {"keyword": "x" * 351},
        {"keyword": "ok", "limit": 0},
        {"keyword": "ok", "limit": 201},
        {"keyword": "ok", "offset": -1},
        {"keyword": "ok", "offset": 10_000},
        {"keyword": "ok", "max_price": 0},
        {"keyword": "ok", "max_price": -5},
        {"keyword": "ok", "max_price": "not-a-number"},
    ],
)
def test_invalid_search_input_is_rejected_before_any_request(kwargs) -> None:
    """Validation happens locally, so a bad request never costs an API call."""
    with pytest.raises(InvalidSearchRequest):
        EbayClient.build_search_params(**kwargs)


def test_sandbox_base_url_is_honoured() -> None:
    """EBAY_API_BASE must redirect both the OAuth and Browse endpoints."""
    sandbox = Settings(
        ebay_client_id="id",
        ebay_client_secret="secret",
        ebay_api_base="https://api.sandbox.ebay.com",
    )
    assert sandbox.token_url == "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    assert (
        sandbox.search_url
        == "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search"
    )


# --------------------------------------------------------------------------- #
# 11. API error handling
# --------------------------------------------------------------------------- #


def _search_handler(status_code: int, **kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(status_code, **kwargs)

    return handler


def test_rate_limited_search_raises_rate_limit_error(make_client) -> None:
    client = make_client(_search_handler(429, json={"errors": [{"message": "slow"}]}))
    with pytest.raises(EbayRateLimitError) as exc_info:
        client.search(keyword="lego")
    assert exc_info.value.status_code == 429


def test_server_error_raises_api_error_with_upstream_message(make_client) -> None:
    client = make_client(
        _search_handler(500, json={"errors": [{"message": "Internal failure"}]})
    )
    with pytest.raises(EbayApiError) as exc_info:
        client.search(keyword="lego")
    assert exc_info.value.status_code == 500
    assert "Internal failure" in exc_info.value.message


def test_malformed_json_raises_response_error(make_client) -> None:
    client = make_client(_search_handler(200, content=b"<html>nope</html>"))
    with pytest.raises(EbayResponseError):
        client.search(keyword="lego")


def test_non_object_search_body_raises_response_error(make_client) -> None:
    client = make_client(_search_handler(200, json=["unexpected", "list"]))
    with pytest.raises(EbayResponseError):
        client.search(keyword="lego")


def test_repeated_401_raises_auth_error_after_one_retry(make_client) -> None:
    """A 401 triggers exactly one token refresh; a second 401 is fatal."""
    calls = {"token": 0, "search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return token_response(token=f"token-{calls['token']}")
        calls["search"] += 1
        return httpx.Response(401, json={"errors": [{"message": "invalid token"}]})

    client = make_client(handler)
    with pytest.raises(EbayAuthError):
        client.search(keyword="lego")

    assert calls["search"] == 2, "should retry the search exactly once"
    assert calls["token"] == 2, "should mint exactly one replacement token"


def test_401_then_success_recovers_transparently(make_client) -> None:
    """An expired cached token is refreshed and the search succeeds."""
    calls = {"search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response(token="fresh")
        calls["search"] += 1
        if calls["search"] == 1:
            return httpx.Response(401, json={"errors": [{"message": "expired"}]})
        return httpx.Response(200, json={"total": 1, "itemSummaries": []})

    client = make_client(handler)
    assert client.search(keyword="lego")["total"] == 1
    assert calls["search"] == 2


def test_oauth_rejection_raises_auth_error(make_client) -> None:
    client = make_client(lambda request: httpx.Response(401, json={"error": "bad"}))
    with pytest.raises(EbayAuthError) as exc_info:
        client.get_access_token()
    assert exc_info.value.status_code == 401
    assert "test-client-secret" not in str(exc_info.value)


def test_oauth_response_without_token_raises_response_error(make_client) -> None:
    client = make_client(lambda request: httpx.Response(200, json={"expires_in": 100}))
    with pytest.raises(EbayResponseError):
        client.get_access_token()


def test_timeout_raises_timeout_error(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = make_client(handler)
    with pytest.raises(EbayTimeoutError):
        client.get_access_token()


def test_connection_failure_raises_api_error(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = make_client(handler)
    with pytest.raises(EbayApiError):
        client.get_access_token()

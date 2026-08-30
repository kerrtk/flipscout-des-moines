"""eBay REST transport.

Scope and conduct
-----------------
This module talks to eBay's **official, documented REST APIs** over HTTPS with
an application access token obtained through the OAuth 2.0 client-credentials
grant. It does not scrape HTML, does not solve or bypass CAPTCHAs, does not
bypass authentication or ``robots.txt``, and does not reach marketplaces that
have not published a supported API. Only query parameters and filters that
appear in eBay's Browse API documentation are sent; nothing is invented.

Credential safety
-----------------
The client secret is used in exactly one place: the Basic auth header of the
token request. It is never logged, never placed in an exception message, and
never returned to a caller. Exceptions raised here carry a status code and a
short upstream message only.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from typing import Any, Final, Self

import httpx

from app.config import EBAY_APPLICATION_SCOPE, Settings, get_settings

logger = logging.getLogger(__name__)

# Refresh a token this many seconds before eBay says it expires, so an
# in-flight request can never race the expiry boundary.
TOKEN_EXPIRY_LEEWAY_SECONDS: Final[float] = 60.0

# Browse API hard limits, per eBay's documentation.
MAX_SEARCH_LIMIT: Final[int] = 200
MAX_SEARCH_OFFSET: Final[int] = 9_999


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class EbayError(Exception):
    """Base class for every eBay transport failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class EbayAuthError(EbayError):
    """eBay rejected our credentials or token (HTTP 401/403)."""


class EbayRateLimitError(EbayError):
    """eBay throttled us (HTTP 429), or the call limit is exhausted."""


class EbayTimeoutError(EbayError):
    """eBay did not respond within REQUEST_TIMEOUT_SECONDS."""


class EbayApiError(EbayError):
    """eBay returned an unexpected status, or the connection failed."""


class EbayResponseError(EbayError):
    """eBay responded, but the body was not the JSON shape we expect."""


class InvalidSearchRequest(ValueError):
    """Caller-supplied search parameters failed validation. Never sent upstream."""


# --------------------------------------------------------------------------- #
# Token cache
# --------------------------------------------------------------------------- #


class _CachedToken:
    """An access token plus the monotonic deadline after which it is stale."""

    __slots__ = ("expires_at", "value")

    def __init__(self, value: str, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at

    def is_valid(self, *, now: float) -> bool:
        return now < self.expires_at

    def __repr__(self) -> str:  # pragma: no cover - never leak the token
        return f"_CachedToken(value='***', expires_at={self.expires_at})"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class EbayClient:
    """Thin, synchronous client for the eBay OAuth and Browse APIs.

    The only mutable state is the cached application token, guarded by a
    ``threading.Lock``. Under concurrent load exactly one thread performs the
    token round-trip; the rest wait on the lock and then observe the freshly
    cached token via the double-checked read inside the critical section.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(self._settings.request_timeout_seconds)
        )
        self._token_lock = threading.Lock()
        self._token: _CachedToken | None = None

    # -- lifecycle ---------------------------------------------------------- #

    def close(self) -> None:
        """Close the underlying HTTP connection pool, if we created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- OAuth -------------------------------------------------------------- #

    def _basic_auth_header(self) -> str:
        """Build ``Basic base64(client_id:client_secret)``.

        Raises:
            MissingConfigurationError: if either credential is absent.
        """
        client_id, client_secret = self._settings.require_credentials()
        raw = f"{client_id}:{client_secret}".encode()
        return f"Basic {base64.b64encode(raw).decode('ascii')}"

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid application access token, minting one only if needed.

        A cached token is reused until ``TOKEN_EXPIRY_LEEWAY_SECONDS`` before
        its expiry, so a warm process makes no OAuth calls at all.
        """
        now = time.monotonic()
        cached = self._token
        if not force_refresh and cached is not None and cached.is_valid(now=now):
            return cached.value

        with self._token_lock:
            # Double-checked: another thread may have refreshed while we waited.
            now = time.monotonic()
            cached = self._token
            if not force_refresh and cached is not None and cached.is_valid(now=now):
                return cached.value

            token, expires_in = self._request_new_token()
            self._token = _CachedToken(
                value=token,
                expires_at=time.monotonic()
                + max(expires_in - TOKEN_EXPIRY_LEEWAY_SECONDS, 0.0),
            )
            return token

    def _request_new_token(self) -> tuple[str, float]:
        """Perform the client-credentials grant. Returns ``(token, expires_in)``."""
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "grant_type": "client_credentials",
            "scope": EBAY_APPLICATION_SCOPE,
        }

        try:
            response = self._client.post(
                self._settings.token_url,
                headers=headers,
                data=data,
                timeout=self._settings.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise EbayTimeoutError(
                "Timed out requesting an eBay OAuth token after "
                f"{self._settings.request_timeout_seconds}s"
            ) from exc
        except httpx.RequestError as exc:
            # str(exc) is httpx's transport message; it never contains our body.
            raise EbayApiError(
                f"Could not reach the eBay OAuth endpoint: {exc.__class__.__name__}"
            ) from exc

        if response.status_code in (401, 403):
            raise EbayAuthError(
                "eBay rejected the application credentials. Verify "
                "EBAY_CLIENT_ID/EBAY_CLIENT_SECRET and that they match "
                "EBAY_API_BASE (production keys do not work against sandbox).",
                status_code=response.status_code,
            )
        if response.status_code == 429:
            raise EbayRateLimitError(
                "eBay rate-limited the OAuth token request.",
                status_code=429,
            )
        if response.status_code >= 400:
            raise EbayApiError(
                f"eBay OAuth token request failed with HTTP {response.status_code}.",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise EbayResponseError("eBay OAuth response was not valid JSON.") from exc

        if not isinstance(payload, dict):
            raise EbayResponseError("eBay OAuth response was not a JSON object.")

        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise EbayResponseError(
                "eBay OAuth response did not contain an access_token."
            )

        expires_in = payload.get("expires_in", 7200)
        try:
            expires_in_seconds = float(expires_in)
        except (TypeError, ValueError):
            expires_in_seconds = 7200.0
        if expires_in_seconds <= 0:
            expires_in_seconds = 7200.0

        logger.debug(
            "Obtained a new eBay application token (expires_in=%ss).",
            expires_in_seconds,
        )
        return token, expires_in_seconds

    # -- Browse search ------------------------------------------------------ #

    @staticmethod
    def build_search_params(
        *,
        keyword: str,
        limit: int = 50,
        offset: int = 0,
        condition: str | None = None,
        max_price: object | None = None,
        currency: str = "USD",
    ) -> dict[str, str]:
        """Validate caller input and build documented Browse query parameters.

        Raises:
            InvalidSearchRequest: if any input is out of range. Validation
                happens *before* any network call, so a bad request never
                consumes an eBay API call.
        """
        cleaned_keyword = (keyword or "").strip()
        if not cleaned_keyword:
            raise InvalidSearchRequest("q must be a non-empty search keyword.")
        if len(cleaned_keyword) > 350:
            raise InvalidSearchRequest("q must be 350 characters or fewer.")

        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise InvalidSearchRequest(f"limit must be between 1 and {MAX_SEARCH_LIMIT}.")
        if not 0 <= offset <= MAX_SEARCH_OFFSET:
            raise InvalidSearchRequest(
                f"offset must be between 0 and {MAX_SEARCH_OFFSET}."
            )

        # buyingOptions asks eBay for both fixed-price and auction inventory.
        # The {A|B} syntax is eBay's documented multi-value filter form.
        filters = ["buyingOptions:{FIXED_PRICE|AUCTION}"]

        if condition:
            filters.append(f"conditions:{{{condition}}}")

        if max_price is not None:
            price = str(max_price)
            try:
                if float(price) <= 0:
                    raise InvalidSearchRequest("max_price must be greater than 0.")
            except (TypeError, ValueError) as exc:
                raise InvalidSearchRequest("max_price must be a number.") from exc
            # eBay requires priceCurrency whenever the price filter is used.
            filters.append(f"price:[..{price}]")
            filters.append(f"priceCurrency:{currency}")

        return {
            "q": cleaned_keyword,
            "limit": str(limit),
            "offset": str(offset),
            "filter": ",".join(filters),
        }

    def search(
        self,
        *,
        keyword: str,
        limit: int = 50,
        offset: int = 0,
        condition: str | None = None,
        max_price: object | None = None,
    ) -> dict[str, Any]:
        """Run a Browse ``item_summary/search`` and return the parsed JSON body.

        The response is the raw eBay envelope (``total``, ``offset``, ``limit``,
        ``itemSummaries``). Turning it into ``NormalizedListing`` objects is the
        normalization layer's job, kept separate so a second marketplace can
        reuse it.
        """
        params = self.build_search_params(
            keyword=keyword,
            limit=limit,
            offset=offset,
            condition=condition,
            max_price=max_price,
        )

        payload = self._get_json(
            self._settings.search_url,
            params=params,
            token=self.get_access_token(),
        )

        if not isinstance(payload, dict):
            raise EbayResponseError("eBay Browse search returned a non-object body.")
        return payload

    def _get_json(self, url: str, *, params: dict[str, str], token: str) -> Any:
        """GET a Browse endpoint, retrying once on a 401 with a fresh token."""
        response = self._send_get(url, params=params, token=token)

        # A 401 on a cached token usually means eBay expired it early. Mint a
        # new one and retry exactly once; a second 401 is a real auth failure.
        if response.status_code == 401:
            logger.debug("eBay returned 401; refreshing the application token once.")
            response = self._send_get(
                url, params=params, token=self.get_access_token(force_refresh=True)
            )

        if response.status_code in (401, 403):
            raise EbayAuthError(
                "eBay rejected the access token for the Browse API. Confirm the "
                "application has Browse API access for this marketplace.",
                status_code=response.status_code,
            )
        if response.status_code == 429:
            raise EbayRateLimitError(
                "eBay rate limit reached for the Browse API. Retry later or "
                "reduce request volume.",
                status_code=429,
            )
        if response.status_code >= 400:
            raise EbayApiError(
                f"eBay Browse API returned HTTP {response.status_code}: "
                f"{_short_error(response)}",
                status_code=response.status_code,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise EbayResponseError(
                "eBay Browse API response was not valid JSON."
            ) from exc

    def _send_get(
        self, url: str, *, params: dict[str, str], token: str
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self._settings.ebay_marketplace_id,
            "Accept": "application/json",
        }
        try:
            return self._client.get(
                url,
                params=params,
                headers=headers,
                timeout=self._settings.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise EbayTimeoutError(
                "Timed out calling the eBay Browse API after "
                f"{self._settings.request_timeout_seconds}s"
            ) from exc
        except httpx.RequestError as exc:
            raise EbayApiError(
                f"Could not reach the eBay Browse API: {exc.__class__.__name__}"
            ) from exc


def _short_error(response: httpx.Response) -> str:
    """Extract a brief upstream error message, truncated and credential-free."""
    try:
        body = response.json()
    except ValueError:
        return "<non-JSON error body>"
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            message = errors[0].get("message") or errors[0].get("longMessage")
            if isinstance(message, str):
                return message[:300]
    return "<no error detail>"

"""FastAPI application: HTTP routing only.

Transport (``services.ebay_client``), normalization
(``services.normalization``), and arithmetic (``services.profitability``) live
in separate modules. This file maps them onto HTTP and translates domain
exceptions into status codes; it contains no business logic of its own.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse

from app import __version__
from app.config import MissingConfigurationError, Settings, get_settings
from app.models import (
    EbayItemCondition,
    ListingSearchResponse,
    NormalizedListing,
    ProfitAssumptions,
    ProfitEstimate,
)
from app.services.ebay_client import (
    EbayApiError,
    EbayAuthError,
    EbayClient,
    EbayError,
    EbayRateLimitError,
    EbayResponseError,
    EbayTimeoutError,
    InvalidSearchRequest,
)
from app.services.normalization import (
    NormalizationError,
    normalize_ebay_item,
    normalize_ebay_search_response,
)
from app.services.profitability import estimate_profit

logger = logging.getLogger(__name__)

DESCRIPTION = """
Backend for **FlipScout Des Moines** — find potentially profitable resale
items and reason about the margin transparently.

### Read this before trusting a number

The eBay Browse API returns **current listings, i.e. asking prices** — not
sold prices. An unsold $900 listing for a $12 item proves only that somebody
asked for $900. Profit figures from this API are arithmetic on assumptions
*you* supply; they are not a claim that an item will sell. A production
resale estimator must source its resale price from authorized sold/completed
comparable data before assigning any estimate or confidence score.

### 5x resale multiple is not 500% ROI

Buying at $100 and reselling at $500 is a **5x multiple** and **400% gross
ROI** — your $100 of capital comes back, only $400 is profit.
`qualifies_for_500_percent_resale_multiple` tests `gross_multiple >= 5` and is
intentionally separate from `gross_roi_percent`.
"""

app = FastAPI(
    title="FlipScout Des Moines API",
    version=__version__,
    description=DESCRIPTION,
    contact={"name": "FlipScout Des Moines"},
)


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _shared_ebay_client() -> EbayClient:
    """One process-wide client so the token cache and connection pool are shared.

    This is the single piece of global mutable state in the application, and it
    is deliberate: a per-request client would re-run the OAuth handshake on
    every search. Its internal cache is lock-guarded (see ``EbayClient``).
    """
    return EbayClient(get_settings())


def get_ebay_client() -> EbayClient:
    """FastAPI dependency. Overridable in tests via ``dependency_overrides``."""
    return _shared_ebay_client()


def get_app_settings() -> Settings:
    return get_settings()


# --------------------------------------------------------------------------- #
# Exception handlers -> HTTP status codes
# --------------------------------------------------------------------------- #


def _problem(status_code: int, error: str, detail: str) -> JSONResponse:
    """Uniform error body. Never contains credentials or upstream headers."""
    return JSONResponse(
        status_code=status_code, content={"error": error, "detail": detail}
    )


@app.exception_handler(MissingConfigurationError)
async def _handle_missing_config(
    _request: Request, exc: MissingConfigurationError
) -> JSONResponse:
    # 503: the server is not configured. The client did nothing wrong and a
    # retry will not help until an operator sets the variable.
    logger.error("Missing configuration: %s", exc.variable)
    return _problem(status.HTTP_503_SERVICE_UNAVAILABLE, "configuration_error", str(exc))


@app.exception_handler(InvalidSearchRequest)
async def _handle_invalid_search(
    _request: Request, exc: InvalidSearchRequest
) -> JSONResponse:
    return _problem(status.HTTP_400_BAD_REQUEST, "invalid_request", str(exc))


@app.exception_handler(NormalizationError)
async def _handle_normalization_error(
    _request: Request, exc: NormalizationError
) -> JSONResponse:
    return _problem(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_payload", str(exc))


@app.exception_handler(EbayRateLimitError)
async def _handle_rate_limit(_request: Request, exc: EbayRateLimitError) -> JSONResponse:
    # 429 rather than a generic 502: rate limiting has a canonical status code
    # and callers can back off on it automatically.
    return _problem(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited", exc.message)


@app.exception_handler(EbayTimeoutError)
async def _handle_timeout(_request: Request, exc: EbayTimeoutError) -> JSONResponse:
    return _problem(status.HTTP_504_GATEWAY_TIMEOUT, "upstream_timeout", exc.message)


@app.exception_handler(EbayAuthError)
async def _handle_auth_error(_request: Request, exc: EbayAuthError) -> JSONResponse:
    # 502, not 401: the caller of *this* API is not the one who failed to
    # authenticate. Our upstream credential exchange did.
    logger.error("eBay authentication failed (upstream status=%s).", exc.status_code)
    return _problem(status.HTTP_502_BAD_GATEWAY, "upstream_auth_error", exc.message)


@app.exception_handler(EbayResponseError)
async def _handle_bad_response(_request: Request, exc: EbayResponseError) -> JSONResponse:
    return _problem(
        status.HTTP_502_BAD_GATEWAY, "upstream_malformed_response", exc.message
    )


@app.exception_handler(EbayApiError)
async def _handle_api_error(_request: Request, exc: EbayApiError) -> JSONResponse:
    return _problem(status.HTTP_502_BAD_GATEWAY, "upstream_error", exc.message)


@app.exception_handler(EbayError)
async def _handle_generic_ebay_error(_request: Request, exc: EbayError) -> JSONResponse:
    return _problem(status.HTTP_502_BAD_GATEWAY, "upstream_error", exc.message)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/health", tags=["system"], summary="Liveness and configuration check")
def health(settings: Annotated[Settings, Depends(get_app_settings)]) -> dict[str, Any]:
    """Report liveness plus whether eBay credentials are present.

    Reports only *whether* credentials are configured — never their values.
    """
    return {
        "status": "ok",
        "version": __version__,
        "ebay_api_base": settings.api_base,
        "ebay_marketplace_id": settings.ebay_marketplace_id,
        "ebay_credentials_configured": bool(
            settings.ebay_client_id and settings.ebay_client_secret
        ),
    }


@app.get(
    "/api/ebay/search",
    response_model=ListingSearchResponse,
    tags=["ebay"],
    summary="Search eBay and return normalized listings",
)
def search_ebay(
    client: Annotated[EbayClient, Depends(get_ebay_client)],
    q: Annotated[
        str, Query(min_length=1, max_length=350, description="Search keywords.")
    ],
    limit: Annotated[int, Query(ge=1, le=200, description="Results per page.")] = 50,
    offset: Annotated[int, Query(ge=0, le=9999, description="Result offset.")] = 0,
    condition: Annotated[
        EbayItemCondition | None,
        Query(description="Optional eBay condition filter."),
    ] = None,
    max_price: Annotated[
        float | None, Query(gt=0, description="Optional maximum item price.")
    ] = None,
) -> ListingSearchResponse:
    """Run a Browse search for fixed-price **and** auction inventory.

    Returns current asking prices. See the API description for why that is not
    the same as sold-comparable data.
    """
    payload = client.search(
        keyword=q,
        limit=limit,
        offset=offset,
        condition=condition.value if condition else None,
        max_price=max_price,
    )

    listings = normalize_ebay_search_response(payload)
    return ListingSearchResponse(
        total=_safe_int(payload.get("total"), default=len(listings)),
        offset=_safe_int(payload.get("offset"), default=offset),
        limit=_safe_int(payload.get("limit"), default=limit),
        listings=listings,
    )


@app.post(
    "/api/normalize/ebay",
    response_model=NormalizedListing,
    tags=["ebay"],
    summary="Normalize a raw eBay item summary",
)
def normalize_ebay(
    raw_item: Annotated[
        dict[str, Any],
        Body(
            description="A raw eBay Browse `itemSummary` object.",
            examples=[
                {
                    "itemId": "v1|1234567890|0",
                    "title": "Vintage Pyrex Mixing Bowl",
                    "price": {"value": "24.99", "currency": "USD"},
                }
            ],
        ),
    ],
) -> NormalizedListing:
    """Convert one eBay item into the marketplace-neutral shape.

    Useful for debugging normalization against real payloads without spending
    an eBay API call.
    """
    return normalize_ebay_item(raw_item)


@app.post(
    "/api/profit-estimate",
    response_model=ProfitEstimate,
    tags=["profit"],
    summary="Compute a transparent profit estimate",
)
def profit_estimate(assumptions: ProfitAssumptions) -> ProfitEstimate:
    """Itemize gross profit, fees, costs, net profit, multiple, and ROI.

    This is arithmetic on the assumptions you supply. It does not verify that
    `resale_price` is achievable — that requires sold-comparable data.
    """
    return estimate_profit(assumptions)


def _safe_int(value: Any, *, default: int) -> int:
    """Coerce an upstream count to a non-negative int, falling back cleanly."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default

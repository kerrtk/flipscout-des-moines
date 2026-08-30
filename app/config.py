"""Environment-driven configuration for the FlipScout backend.

Every secret is read from the process environment. Nothing in this module is
ever serialized into an API response: the eBay client secret is wrapped in a
``pydantic.SecretStr`` so that accidental ``repr()``/logging of the settings
object prints ``**********`` instead of the credential itself.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator

DEFAULT_EBAY_API_BASE = "https://api.ebay.com"
DEFAULT_MARKETPLACE_ID = "EBAY_US"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0

# eBay's OAuth scope for application (client-credentials) tokens. The Browse
# API's public search surface only requires this base scope.
EBAY_APPLICATION_SCOPE = "https://api.ebay.com/oauth/api_scope"

# Paths are documented eBay REST routes, joined onto EBAY_API_BASE so that a
# sandbox host can be swapped in with a single environment variable.
# The S105 suppression below is deliberate: this is a public URL path, not
# a credential.
OAUTH_TOKEN_PATH = "/identity/v1/oauth2/token"  # noqa: S105
BROWSE_SEARCH_PATH = "/buy/browse/v1/item_summary/search"


class MissingConfigurationError(RuntimeError):
    """Raised when a required environment variable is absent or blank.

    Surfaced to clients as HTTP 503 — the server, not the caller, is
    misconfigured. The message names only the missing variable, never a value.
    """

    def __init__(self, variable: str) -> None:
        self.variable = variable
        super().__init__(
            f"Missing required environment variable: {variable}. "
            "See .env.example for the expected configuration."
        )


class Settings(BaseModel):
    """Validated runtime settings.

    ``ebay_client_id``/``ebay_client_secret`` are optional at construction time
    so that the process can boot (and serve ``/health`` plus the offline
    normalization and profit endpoints) without eBay credentials. The credential
    check happens lazily, when an eBay call is actually attempted.
    """

    model_config = {"frozen": True}

    ebay_client_id: str | None = None
    ebay_client_secret: SecretStr | None = None
    ebay_api_base: HttpUrl = Field(default=HttpUrl(DEFAULT_EBAY_API_BASE))
    ebay_marketplace_id: str = Field(default=DEFAULT_MARKETPLACE_ID, min_length=1)
    request_timeout_seconds: float = Field(
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS, gt=0, le=300
    )

    @field_validator("ebay_client_id", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Treat an empty/whitespace-only env var as "not configured"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def api_base(self) -> str:
        """The eBay API host with any trailing slash removed."""
        return str(self.ebay_api_base).rstrip("/")

    @property
    def token_url(self) -> str:
        return f"{self.api_base}{OAUTH_TOKEN_PATH}"

    @property
    def search_url(self) -> str:
        return f"{self.api_base}{BROWSE_SEARCH_PATH}"

    def require_credentials(self) -> tuple[str, str]:
        """Return ``(client_id, client_secret)`` or raise.

        Raises:
            MissingConfigurationError: if either credential is unset.
        """
        if not self.ebay_client_id:
            raise MissingConfigurationError("EBAY_CLIENT_ID")
        if (
            self.ebay_client_secret is None
            or not self.ebay_client_secret.get_secret_value()
        ):
            raise MissingConfigurationError("EBAY_CLIENT_SECRET")
        return self.ebay_client_id, self.ebay_client_secret.get_secret_value()

    def __repr__(self) -> str:  # pragma: no cover - defensive nicety
        return (
            f"Settings(ebay_api_base={self.api_base!r}, "
            f"ebay_marketplace_id={self.ebay_marketplace_id!r}, "
            f"request_timeout_seconds={self.request_timeout_seconds!r}, "
            f"ebay_client_id={'set' if self.ebay_client_id else 'unset'}, "
            f"ebay_client_secret={'set' if self.ebay_client_secret else 'unset'})"
        )

    __str__ = __repr__


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to ``default`` when blank."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise MissingConfigurationError(
            f"{name} (expected a number, got an unparsable value)"
        ) from exc


def load_settings() -> Settings:
    """Build ``Settings`` from the current process environment."""
    secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    return Settings(
        ebay_client_id=os.environ.get("EBAY_CLIENT_ID"),
        ebay_client_secret=SecretStr(secret) if secret else None,
        ebay_api_base=os.environ.get("EBAY_API_BASE", "").strip()
        or DEFAULT_EBAY_API_BASE,
        ebay_marketplace_id=(
            os.environ.get("EBAY_MARKETPLACE_ID", "").strip() or DEFAULT_MARKETPLACE_ID
        ),
        request_timeout_seconds=_env_float(
            "REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings.

    Cached so that request handling does not re-parse the environment on every
    call. Tests that mutate ``os.environ`` must call
    ``get_settings.cache_clear()`` afterwards.
    """
    return load_settings()

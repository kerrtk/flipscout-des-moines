"""Saved searches and routes, loaded from a hand-edited YAML file.

A daily scan needs named, persistent queries rather than ad-hoc ones. This
module is the config seam: the scanner iterates whatever the watchlist
defines, so tuning what you hunt for never means touching code.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import EbayItemCondition
from app.services.geo import Base, Route

DEFAULT_WATCHLIST_PATH = Path("watchlist.yaml")


class WatchlistError(ValueError):
    """The watchlist file is missing, unparsable, or fails validation."""


class SavedSearch(BaseModel):
    """One named query the scanner runs on every pass."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    q: str = Field(min_length=1, max_length=350)
    enabled: bool = True

    limit: int = Field(default=50, ge=1, le=200)
    condition: EbayItemCondition | None = None
    max_price: Decimal | None = Field(default=None, gt=0)

    # Sourcing mode. local_pickup_only is the box-truck play: items that
    # cannot ship are priced for a tiny local buyer pool, which is exactly
    # the inefficiency a truck monetizes.
    local_pickup_only: bool = False

    #: Route names this search is scoped to. Empty means statewide anchors.
    routes: list[str] = Field(default_factory=list)

    #: Minimum resale multiple worth surfacing at all.
    min_multiple: Decimal = Field(default=Decimal("3"), gt=0)

    #: Rough resale estimate, used only until sold-comps data is wired in.
    #: See services.profitability - an asking price is NOT a resale price.
    assumed_resale_price: Decimal | None = Field(default=None, gt=0)

    #: Estimated weight/bulk class, for truck-capacity planning.
    bulk: str | None = Field(default=None, pattern="^(small|medium|large|pallet)$")

    notes: str | None = None


class PickupWindow(BaseModel):
    """A recurring block of time you can actually collect something in."""

    model_config = ConfigDict(extra="forbid")

    day: str = Field(pattern="^(mon|tue|wed|thu|fri|sat|sun)$")
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def _end_after_start(self) -> PickupWindow:
        if self.end <= self.start:
            raise ValueError(
                f"window {self.day} {self.start}-{self.end}: end must be after start"
            )
        return self

    def minutes_long(self) -> int:
        def to_minutes(value: str) -> int:
            hours, minutes = value.split(":")
            return int(hours) * 60 + int(minutes)

        return to_minutes(self.end) - to_minutes(self.start)


class Availability(BaseModel):
    """When you can collect, and how far you will go on a work night.

    ``max_pickup_minutes`` is ROUND-TRIP driving time, not one way. A 45
    minute budget is a ~22 minute drive each way - which at highway speed is
    roughly 20 miles, and much less in town. Being honest about this is what
    stops the scanner surfacing things you will never actually go get.
    """

    model_config = ConfigDict(extra="forbid")

    max_pickup_minutes: int = Field(default=45, gt=0, le=600)
    average_speed_mph: float = Field(default=35.0, gt=0, le=90)
    windows: list[PickupWindow] = Field(default_factory=list)

    def max_round_trip_miles(self) -> float:
        return (self.max_pickup_minutes / 60.0) * self.average_speed_mph

    def max_one_way_miles(self) -> float:
        return self.max_round_trip_miles() / 2

    def weekly_minutes(self) -> int:
        return sum(window.minutes_long() for window in self.windows)


class Watchlist(BaseModel):
    """The whole config: routes plus the searches that reference them."""

    model_config = ConfigDict(extra="forbid")

    routes: list[Route] = Field(default_factory=list)
    bases: list[Base] = Field(default_factory=list)
    availability: Availability = Field(default_factory=Availability)
    searches: list[SavedSearch] = Field(default_factory=list)

    @model_validator(mode="after")
    def _routes_referenced_must_exist(self) -> Watchlist:
        known = {route.name for route in self.routes}
        for search in self.searches:
            unknown = set(search.routes) - known
            if unknown:
                raise ValueError(
                    f"search {search.name!r} references unknown route(s): "
                    f"{', '.join(sorted(unknown))}. Known routes: "
                    f"{', '.join(sorted(known)) or '(none)'}"
                )
        return self

    def route_by_name(self, name: str) -> Route | None:
        return next((route for route in self.routes if route.name == name), None)

    def active_searches(self) -> list[SavedSearch]:
        return [search for search in self.searches if search.enabled]

    def routes_for(self, search: SavedSearch) -> list[Route]:
        """Routes a search is scoped to; all routes when it names none."""
        if not search.routes:
            return list(self.routes)
        return [r for name in search.routes if (r := self.route_by_name(name))]


def load_watchlist(path: Path | str = DEFAULT_WATCHLIST_PATH) -> Watchlist:
    """Parse and validate a watchlist file.

    Raises:
        WatchlistError: on a missing file, malformed YAML, or invalid schema -
            with the offending file named, so a 5am cron failure is diagnosable
            from the log line alone.
    """
    path = Path(path)
    if not path.is_file():
        raise WatchlistError(
            f"Watchlist not found at {path}. Copy watchlist.example.yaml to "
            f"{path} and edit it."
        )

    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise WatchlistError(f"{path} is not valid YAML: {exc}") from exc

    if raw is None:
        raise WatchlistError(f"{path} is empty.")
    if not isinstance(raw, dict):
        raise WatchlistError(
            f"{path} must contain a mapping with 'routes' and 'searches' keys."
        )

    try:
        return Watchlist.model_validate(raw)
    except ValueError as exc:
        raise WatchlistError(f"{path} failed validation: {exc}") from exc

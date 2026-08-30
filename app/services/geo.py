"""Geography for route-based sourcing.

The organizing idea: an item's real cost includes getting it home. A washer
90 miles off your route is not the same deal as an identical washer 3 miles
off it, even at the same price. This module measures that difference so the
scanner can price it in.

Distance is computed against the *route*, not against a home base. A single
radius around Des Moines would miss everything along the Sioux City corridor
and would wrongly include things 60 miles the wrong way. What matters is
perpendicular distance to the path already being driven.

Accuracy note: distances use a spherical earth and, for point-to-segment,
a local equirectangular projection. Over Iowa-scale spans (a few hundred
miles) the error is well under a mile - far tighter than the uncertainty in
"how far off the highway is this actually" - and the alternative (a routing
API) costs a network call per item.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field

EARTH_RADIUS_MILES = 3958.7613


class Waypoint(BaseModel):
    """A named stop on a route."""

    model_config = ConfigDict(extra="forbid")

    name: str
    postal_code: str | None = None
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    @property
    def point(self) -> tuple[float, float]:
        return (self.lat, self.lon)


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in statute miles between two (lat, lon) points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, math.sqrt(h) ** 2)))


def _project(point: tuple[float, float], origin_lat_rad: float) -> tuple[float, float]:
    """Equirectangular projection to (x, y) miles, valid near ``origin_lat``."""
    lat, lon = point
    x = math.radians(lon) * math.cos(origin_lat_rad) * EARTH_RADIUS_MILES
    y = math.radians(lat) * EARTH_RADIUS_MILES
    return (x, y)


def point_to_segment_miles(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    """Shortest distance in miles from ``point`` to the segment ``start``-``end``.

    A degenerate segment (start == end) falls back to plain point distance.
    """
    origin_lat_rad = math.radians((start[0] + end[0]) / 2)
    px, py = _project(point, origin_lat_rad)
    ax, ay = _project(start, origin_lat_rad)
    bx, by = _project(end, origin_lat_rad)

    dx, dy = bx - ax, by - ay
    segment_length_sq = dx * dx + dy * dy
    if segment_length_sq == 0:
        return haversine_miles(point, start)

    # Projection parameter, clamped to the segment so we never measure to a
    # point on the infinite line that lies beyond either endpoint.
    t = ((px - ax) * dx + (py - ay) * dy) / segment_length_sq
    t = max(0.0, min(1.0, t))
    nearest_x, nearest_y = ax + t * dx, ay + t * dy
    return math.hypot(px - nearest_x, py - nearest_y)


class Route(BaseModel):
    """An ordered set of waypoints you already drive.

    ``max_detour_miles`` is the one-way tolerance off the path. The scanner
    charges round-trip mileage against a deal, because you have to come back.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    waypoints: list[Waypoint] = Field(min_length=1)
    max_detour_miles: float = Field(default=35.0, gt=0, le=500)
    cadence_days: int | None = Field(default=None, gt=0)
    notes: str | None = None

    def detour_miles(self, point: tuple[float, float]) -> float:
        """One-way miles from ``point`` to the nearest part of the route."""
        points = [wp.point for wp in self.waypoints]
        if len(points) == 1:
            return haversine_miles(point, points[0])
        return min(
            point_to_segment_miles(point, start, end) for start, end in pairwise(points)
        )

    def nearest_waypoint(self, point: tuple[float, float]) -> Waypoint:
        """The named stop closest to ``point`` - useful for human-readable output."""
        return min(self.waypoints, key=lambda wp: haversine_miles(point, wp.point))

    def is_on_route(self, point: tuple[float, float]) -> bool:
        return self.detour_miles(point) <= self.max_detour_miles

    def anchor_postal_codes(self) -> list[str]:
        """Postal codes to use as eBay local-pickup search anchors.

        eBay's pickup filter takes a single postal code plus a radius, so a
        corridor has to be searched as several overlapping circles rather than
        one query.
        """
        return [wp.postal_code for wp in self.waypoints if wp.postal_code]


def total_route_miles(waypoints: Sequence[Waypoint]) -> float:
    """Straight-line length of the route, for rough fuel math."""
    points = [wp.point for wp in waypoints]
    return sum(haversine_miles(start, end) for start, end in pairwise(points))


def filter_on_route(
    items: Iterable[tuple[float, float]], route: Route
) -> list[tuple[float, float]]:
    """Keep only points within the route's detour tolerance."""
    return [point for point in items if route.is_on_route(point)]


class Base(BaseModel):
    """A place you actually are during the week - home, work, a job site.

    Distinct from a Route waypoint: a base is a *start point* for a short
    round trip squeezed into a lunch break or an evening, not a stop on a
    drive you were already making.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    postal_code: str | None = None
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    @property
    def point(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    def miles_to(self, point: tuple[float, float]) -> float:
        return haversine_miles(self.point, point)


def nearest_base(
    point: tuple[float, float], bases: Sequence[Base]
) -> tuple[Base | None, float | None]:
    """The base closest to ``point``, and how far away it is."""
    if not bases:
        return (None, None)
    best = min(bases, key=lambda base: base.miles_to(point))
    return (best, best.miles_to(point))


class ServiceArea(BaseModel):
    """A radius you already cover for work.

    Different from a Route: a route is a specific drive on a specific day; a
    service area is territory you move through routinely without planning it.
    For a field technician the whole territory is effectively "on the way",
    so anything inside it costs far less to collect than the raw mileage
    suggests.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    center: Waypoint
    radius_miles: float = Field(gt=0, le=1000)
    notes: str | None = None

    def contains(self, point: tuple[float, float]) -> bool:
        return self.miles_from_center(point) <= self.radius_miles

    def miles_from_center(self, point: tuple[float, float]) -> float:
        return haversine_miles(self.center.point, point)


class Helper(BaseModel):
    """Someone who can collect on your behalf.

    A coworker who passes a listing on their commute converts a 60-mile round
    trip into a favour and a coffee. The scanner charges ``favor_cost`` rather
    than mileage, because what you actually spend is social capital, not fuel.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    #: How far this person will realistically go out of their way.
    max_detour_miles: float = Field(default=15.0, gt=0, le=200)
    #: What you book against a deal for asking. Keeps the tool honest about
    #: the fact that favours are finite and not actually free.
    favor_cost: float = Field(default=15.0, ge=0)
    notes: str | None = None

    @property
    def point(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    def can_collect(self, point: tuple[float, float]) -> bool:
        return haversine_miles(self.point, point) <= self.max_detour_miles

    def miles_to(self, point: tuple[float, float]) -> float:
        return haversine_miles(self.point, point)


def nearest_helper(
    point: tuple[float, float], helpers: Sequence[Helper]
) -> tuple[Helper | None, float | None]:
    """The helper closest to ``point`` who could actually collect it."""
    reachable = [h for h in helpers if h.can_collect(point)]
    if not reachable:
        return (None, None)
    best = min(reachable, key=lambda h: h.miles_to(point))
    return (best, best.miles_to(point))

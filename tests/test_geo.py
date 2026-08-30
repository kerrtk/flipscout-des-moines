"""Route geometry: the numbers that decide whether a drive is worth it."""

from __future__ import annotations

import pytest

from app.services.geo import (
    Route,
    Waypoint,
    haversine_miles,
    point_to_segment_miles,
    total_route_miles,
)

DES_MOINES = (41.5868, -93.6250)
SIOUX_CITY = (42.4963, -96.4049)
CARROLL = (42.0653, -94.8669)
CEDAR_RAPIDS = (41.9779, -91.6656)


@pytest.fixture
def corridor() -> Route:
    return Route(
        name="sioux-city",
        max_detour_miles=35,
        waypoints=[
            Waypoint(name="Des Moines", postal_code="50309", lat=41.5868, lon=-93.6250),
            Waypoint(name="Carroll", postal_code="51401", lat=42.0653, lon=-94.8669),
            Waypoint(name="Sioux City", postal_code="51101", lat=42.4963, lon=-96.4049),
        ],
    )


def test_haversine_matches_known_distance() -> None:
    """Des Moines to Sioux City is ~156 straight-line miles (~200 driving)."""
    assert haversine_miles(DES_MOINES, SIOUX_CITY) == pytest.approx(156, abs=4)


def test_haversine_is_zero_for_identical_points() -> None:
    assert haversine_miles(DES_MOINES, DES_MOINES) == pytest.approx(0, abs=0.01)


def test_haversine_is_symmetric() -> None:
    assert haversine_miles(DES_MOINES, SIOUX_CITY) == pytest.approx(
        haversine_miles(SIOUX_CITY, DES_MOINES), abs=0.01
    )


def test_point_on_the_line_has_near_zero_detour() -> None:
    """Carroll sits essentially on the Des Moines-Sioux City diagonal."""
    assert point_to_segment_miles(CARROLL, DES_MOINES, SIOUX_CITY) < 8


def test_segment_distance_clamps_past_the_endpoints() -> None:
    """A point beyond the segment measures to the endpoint, not the line.

    Without clamping, somewhere far west of Sioux City would look "on route"
    because it is near the infinite line through both cities.
    """
    far_west = (42.4963, -99.0)
    to_segment = point_to_segment_miles(far_west, DES_MOINES, SIOUX_CITY)
    to_endpoint = haversine_miles(far_west, SIOUX_CITY)
    assert to_segment == pytest.approx(to_endpoint, rel=0.02)


def test_degenerate_segment_falls_back_to_point_distance() -> None:
    assert point_to_segment_miles(CARROLL, DES_MOINES, DES_MOINES) == pytest.approx(
        haversine_miles(CARROLL, DES_MOINES), rel=0.02
    )


def test_on_route_town_is_accepted(corridor: Route) -> None:
    assert corridor.is_on_route(CARROLL) is True
    assert corridor.detour_miles(CARROLL) < 10


def test_wrong_direction_city_is_rejected(corridor: Route) -> None:
    """Cedar Rapids is the opposite way; a naive radius would include it."""
    assert corridor.is_on_route(CEDAR_RAPIDS) is False
    assert corridor.detour_miles(CEDAR_RAPIDS) > 90


def test_detour_uses_the_nearest_segment_not_the_nearest_endpoint(
    corridor: Route,
) -> None:
    """A town between two stops is close to the path even if far from a stop."""
    between = (41.83, -94.10)  # near Perry, between Des Moines and Carroll
    assert corridor.detour_miles(between) < 15
    assert min(
        haversine_miles(between, wp.point) for wp in corridor.waypoints
    ) > corridor.detour_miles(between)


def test_nearest_waypoint_names_the_right_stop(corridor: Route) -> None:
    assert corridor.nearest_waypoint((42.49, -96.40)).name == "Sioux City"
    assert corridor.nearest_waypoint((41.59, -93.62)).name == "Des Moines"


def test_anchor_postal_codes_feed_the_pickup_search(corridor: Route) -> None:
    assert corridor.anchor_postal_codes() == ["50309", "51401", "51101"]


def test_waypoint_without_postal_code_is_skipped_as_an_anchor() -> None:
    route = Route(
        name="r",
        waypoints=[
            Waypoint(name="A", postal_code="50309", lat=41.5, lon=-93.6),
            Waypoint(name="B", lat=42.0, lon=-94.0),
        ],
    )
    assert route.anchor_postal_codes() == ["50309"]


def test_single_waypoint_route_measures_plain_distance() -> None:
    route = Route(name="one", waypoints=[Waypoint(name="DSM", lat=41.5868, lon=-93.6250)])
    assert route.detour_miles(SIOUX_CITY) == pytest.approx(156, abs=4)


def test_total_route_miles_sums_the_legs(corridor: Route) -> None:
    total = total_route_miles(corridor.waypoints)
    assert total > haversine_miles(DES_MOINES, SIOUX_CITY)
    assert total == pytest.approx(158, abs=8)


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_out_of_range_coordinates_are_rejected(lat, lon) -> None:
    with pytest.raises(ValueError):
        Waypoint(name="bad", lat=lat, lon=lon)

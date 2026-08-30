"""Scan orchestration, route economics, and watchlist validation. All offline."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.services.ebay_client import EbayClient, InvalidSearchRequest
from app.services.geo import Route, Waypoint
from app.services.normalization import normalize_ebay_item
from app.services.scanner import (
    TripEconomics,
    format_report,
    locate,
    scan,
    score_candidate,
)
from app.services.watchlist import SavedSearch, Watchlist, WatchlistError, load_watchlist
from app.storage import Storage
from tests.conftest import token_response

CORRIDOR = Route(
    name="sioux-city",
    max_detour_miles=35,
    waypoints=[
        Waypoint(name="Des Moines", postal_code="50309", lat=41.5868, lon=-93.6250),
        Waypoint(name="Carroll", postal_code="51401", lat=42.0653, lon=-94.8669),
        Waypoint(name="Sioux City", postal_code="51101", lat=42.4963, lon=-96.4049),
    ],
)


def make_item(item_id: str, price: str, lat: float | None, lon: float | None) -> dict:
    location: dict = {"city": "Somewhere", "stateOrProvince": "IA", "country": "US"}
    if lat is not None:
        location["latitude"] = lat
        location["longitude"] = lon
    return {
        "itemId": item_id,
        "title": f"Item {item_id}",
        "itemWebUrl": f"https://www.ebay.com/itm/{item_id}",
        "price": {"value": price, "currency": "USD"},
        "itemLocation": location,
        "buyingOptions": ["FIXED_PRICE"],
    }


# --------------------------------------------------------------------------- #
# Trip economics
# --------------------------------------------------------------------------- #


def test_box_truck_fuel_cost_is_not_a_sedan_cost() -> None:
    """100 miles at 10 mpg and $3.20/gal is $32, not $8."""
    economics = TripEconomics(
        miles_per_gallon=Decimal("10"), fuel_price_per_gallon=Decimal("3.20")
    )
    assert economics.fuel_cost_for(Decimal("100")) == Decimal("32.00")


def test_time_cost_scales_with_distance() -> None:
    economics = TripEconomics(
        hourly_value_of_time=Decimal("25"), average_speed_mph=Decimal("50")
    )
    assert economics.time_cost_for(Decimal("100")) == Decimal("50")


# --------------------------------------------------------------------------- #
# Locating a listing against routes
# --------------------------------------------------------------------------- #


def test_listing_on_the_corridor_is_located() -> None:
    listing = normalize_ebay_item(make_item("a", "50", 42.0653, -94.8669))
    route, stop, detour = locate(listing, [CORRIDOR])
    assert route is not None and route.name == "sioux-city"
    assert stop is not None and stop.name == "Carroll"
    assert detour is not None and detour < 10


def test_listing_without_coordinates_is_not_guessed() -> None:
    """eBay often omits lat/lon; inventing a location would be worse."""
    listing = normalize_ebay_item(make_item("b", "50", None, None))
    assert locate(listing, [CORRIDOR]) == (None, None, None)


def test_locate_picks_the_closest_of_several_routes() -> None:
    other = Route(
        name="east",
        waypoints=[
            Waypoint(name="Cedar Rapids", lat=41.9779, lon=-91.6656),
            Waypoint(name="Davenport", lat=41.5236, lon=-90.5776),
        ],
    )
    listing = normalize_ebay_item(make_item("c", "50", 41.90, -91.30))
    route, _stop, _detour = locate(listing, [CORRIDOR, other])
    assert route is not None and route.name == "east"


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def base_search(**overrides) -> SavedSearch:
    fields = {
        "name": "s",
        "q": "table saw",
        "assumed_resale_price": Decimal("700"),
        "min_multiple": Decimal("3"),
    }
    fields.update(overrides)
    return SavedSearch(**fields)


def test_a_dedicated_trip_costs_more_than_an_on_route_detour() -> None:
    """Same item, same detour - the off-route version must score lower."""
    listing = normalize_ebay_item(make_item("a", "100", 42.0, -94.8))
    economics = TripEconomics()

    _est_on, score_on, cost_on, _r = score_candidate(
        listing,
        base_search(),
        detour_miles=20.0,
        economics=economics,
        on_planned_route=True,
    )
    _est_off, score_off, cost_off, _r2 = score_candidate(
        listing,
        base_search(),
        detour_miles=20.0,
        economics=economics,
        on_planned_route=False,
    )
    assert cost_off > cost_on
    assert score_off < score_on


def test_item_below_min_multiple_scores_zero() -> None:
    """Resale $700 on a $400 ask is 1.75x - under a 3x floor."""
    listing = normalize_ebay_item(make_item("a", "400", 42.0, -94.8))
    estimate, score, _cost, reasons = score_candidate(
        listing,
        base_search(),
        detour_miles=5.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    assert estimate is not None
    assert score == 0
    assert any("below min_multiple" in r for r in reasons)


def test_search_without_resale_estimate_refuses_to_score() -> None:
    """No resale figure means no opinion - not a fabricated one."""
    listing = normalize_ebay_item(make_item("a", "50", 42.0, -94.8))
    estimate, score, _cost, reasons = score_candidate(
        listing,
        base_search(assumed_resale_price=None),
        detour_miles=5.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    assert estimate is None
    assert score == 0
    assert any("no resale estimate" in r for r in reasons)


def test_listing_without_a_price_scores_zero() -> None:
    listing = normalize_ebay_item({"itemId": "x", "price": {"value": "abc"}})
    _est, score, _cost, reasons = score_candidate(
        listing,
        base_search(),
        detour_miles=1.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    assert score == 0
    assert "no usable asking price" in reasons


def test_drive_cost_reduces_net_profit() -> None:
    """The whole point: distance is charged against the deal."""
    listing = normalize_ebay_item(make_item("a", "100", 42.0, -94.8))
    near, _s1, _c1, _r1 = score_candidate(
        listing,
        base_search(),
        detour_miles=1.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    far, _s2, _c2, _r2 = score_candidate(
        listing,
        base_search(),
        detour_miles=150.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    assert near is not None and far is not None
    assert far.net_profit < near.net_profit


def test_score_is_profit_per_mile_so_close_deals_win() -> None:
    """Two equal-profit items: the closer one must rank first."""
    close = normalize_ebay_item(make_item("a", "100", 42.0, -94.8))
    _e1, score_close, _c1, _r1 = score_candidate(
        close,
        base_search(),
        detour_miles=2.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    _e2, score_far, _c2, _r2 = score_candidate(
        close,
        base_search(),
        detour_miles=30.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    assert score_close > score_far


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #


def test_watchlist_rejects_a_search_naming_an_unknown_route() -> None:
    with pytest.raises(ValueError, match="unknown route"):
        Watchlist(routes=[CORRIDOR], searches=[base_search(routes=["nope"])])


def test_search_without_routes_uses_all_routes() -> None:
    watchlist = Watchlist(routes=[CORRIDOR], searches=[base_search()])
    assert watchlist.routes_for(watchlist.searches[0]) == [CORRIDOR]


def test_disabled_searches_are_excluded() -> None:
    watchlist = Watchlist(
        routes=[CORRIDOR],
        searches=[base_search(name="on"), base_search(name="off", enabled=False)],
    )
    assert [s.name for s in watchlist.active_searches()] == ["on"]


def test_missing_watchlist_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(WatchlistError, match="not found"):
        load_watchlist(tmp_path / "absent.yaml")


def test_malformed_yaml_is_reported_clearly(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("routes: [\n  unclosed")
    with pytest.raises(WatchlistError, match="not valid YAML"):
        load_watchlist(path)


def test_empty_watchlist_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("")
    with pytest.raises(WatchlistError, match="empty"):
        load_watchlist(path)


def test_the_shipped_example_watchlist_is_valid() -> None:
    """The file users copy must actually parse."""
    watchlist = load_watchlist(Path(__file__).parent.parent / "watchlist.example.yaml")
    assert watchlist.routes
    assert watchlist.active_searches()
    for search in watchlist.active_searches():
        assert search.assumed_resale_price is not None


# --------------------------------------------------------------------------- #
# Pickup search construction
# --------------------------------------------------------------------------- #


def test_pickup_search_builds_all_four_filter_parts() -> None:
    filters = EbayClient.build_search_params(
        keyword="saw",
        local_pickup_only=True,
        pickup_postal_code="51401",
        pickup_radius_miles=35,
    )["filter"].split(",")
    assert "pickupCountry:US" in filters
    assert "pickupPostalCode:51401" in filters
    assert "pickupRadius:35" in filters
    assert "pickupRadiusUnit:mi" in filters


def test_pickup_search_without_a_postal_code_is_rejected() -> None:
    with pytest.raises(InvalidSearchRequest, match="pickup_postal_code"):
        EbayClient.build_search_params(keyword="saw", local_pickup_only=True)


@pytest.mark.parametrize("radius", [0, 501])
def test_invalid_pickup_radius_is_rejected(radius) -> None:
    with pytest.raises(InvalidSearchRequest, match="pickup_radius_miles"):
        EbayClient.build_search_params(
            keyword="saw", pickup_postal_code="51401", pickup_radius_miles=radius
        )


def test_non_pickup_search_has_no_pickup_filters() -> None:
    assert "pickup" not in EbayClient.build_search_params(keyword="saw")["filter"]


# --------------------------------------------------------------------------- #
# End-to-end scan (mocked transport)
# --------------------------------------------------------------------------- #


def make_client(handler) -> EbayClient:
    settings = Settings(ebay_client_id="id", ebay_client_secret="secret")
    return EbayClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_scan_surfaces_a_qualifying_on_route_item(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={
                "total": 1,
                "itemSummaries": [make_item("v1|good|0", "80", 42.0653, -94.8669)],
            },
        )

    watchlist = Watchlist(routes=[CORRIDOR], searches=[base_search()])
    with Storage(tmp_path / "s.db") as storage:
        result = scan(make_client(handler), watchlist, storage)

    assert result.searches_run == 1
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.nearest_stop == "Carroll"
    assert candidate.route_name == "sioux-city"
    assert candidate.estimate is not None
    assert candidate.estimate.gross_multiple >= Decimal("3")


def test_second_scan_does_not_resurface_the_same_item(tmp_path: Path) -> None:
    """This is what keeps a daily email readable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={"itemSummaries": [make_item("v1|same|0", "80", 42.0653, -94.8669)]},
        )

    watchlist = Watchlist(routes=[CORRIDOR], searches=[base_search()])
    db = tmp_path / "s.db"
    with Storage(db) as storage:
        first = scan(make_client(handler), watchlist, storage)
    with Storage(db) as storage:
        second = scan(make_client(handler), watchlist, storage)

    assert len(first.candidates) == 1
    assert second.items_new == 0
    assert second.candidates == []


def test_include_seen_resurfaces_previous_items(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={"itemSummaries": [make_item("v1|same|0", "80", 42.0653, -94.8669)]},
        )

    watchlist = Watchlist(routes=[CORRIDOR], searches=[base_search()])
    db = tmp_path / "s.db"
    with Storage(db) as storage:
        scan(make_client(handler), watchlist, storage)
    with Storage(db) as storage:
        again = scan(make_client(handler), watchlist, storage, include_seen=True)
    assert len(again.candidates) == 1


def test_rejected_items_never_come_back(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={"itemSummaries": [make_item("v1|junk|0", "80", 42.0653, -94.8669)]},
        )

    watchlist = Watchlist(routes=[CORRIDOR], searches=[base_search()])
    with Storage(tmp_path / "s.db") as storage:
        storage.set_verdict("ebay", "v1|junk|0", "reject", "cracked frame")
        result = scan(make_client(handler), watchlist, storage)
    assert result.candidates == []


def test_one_failing_search_does_not_abort_the_scan(tmp_path: Path) -> None:
    """A 500 on one query must not cost you the rest of the morning's report."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"errors": [{"message": "boom"}]})
        return httpx.Response(
            200,
            json={"itemSummaries": [make_item("v1|ok|0", "80", 42.0653, -94.8669)]},
        )

    watchlist = Watchlist(
        routes=[CORRIDOR],
        searches=[base_search(name="first"), base_search(name="second")],
    )
    with Storage(tmp_path / "s.db") as storage:
        result = scan(make_client(handler), watchlist, storage)

    assert len(result.errors) == 1
    assert result.searches_run == 2
    assert len(result.candidates) == 1


def test_local_pickup_search_fans_out_over_every_anchor(tmp_path: Path) -> None:
    """A corridor is several pickup circles, not one - one call per anchor."""
    seen_postal_codes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        for part in request.url.params["filter"].split(","):
            if part.startswith("pickupPostalCode:"):
                seen_postal_codes.append(part.split(":", 1)[1])
        return httpx.Response(200, json={"itemSummaries": []})

    watchlist = Watchlist(
        routes=[CORRIDOR], searches=[base_search(local_pickup_only=True)]
    )
    with Storage(tmp_path / "s.db") as storage:
        result = scan(make_client(handler), watchlist, storage)

    assert seen_postal_codes == ["50309", "51401", "51101"]
    assert result.api_calls == 3


def test_report_warns_that_prices_are_asking_prices(tmp_path: Path) -> None:
    """The caveat must survive into the artifact a human actually reads."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={"itemSummaries": [make_item("v1|a|0", "80", 42.0653, -94.8669)]},
        )

    watchlist = Watchlist(routes=[CORRIDOR], searches=[base_search()])
    with Storage(tmp_path / "s.db") as storage:
        report = format_report(scan(make_client(handler), watchlist, storage))

    assert "ASKING" in report
    assert "sold-comp" in report


def test_report_handles_an_empty_scan(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(200, json={"itemSummaries": []})

    watchlist = Watchlist(routes=[CORRIDOR], searches=[base_search()])
    with Storage(tmp_path / "s.db") as storage:
        report = format_report(scan(make_client(handler), watchlist, storage))
    assert "No candidates" in report

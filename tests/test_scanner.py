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


# --------------------------------------------------------------------------- #
# Pickup tiers - when TIME, not distance, is the binding constraint
# --------------------------------------------------------------------------- #

from app.services.geo import Base  # noqa: E402
from app.services.scanner import classify_pickup  # noqa: E402
from app.services.watchlist import Availability, PickupWindow  # noqa: E402

HOME = Base(name="home", postal_code="50309", lat=41.5868, lon=-93.6250)
WORK = Base(name="work", lat=41.6000, lon=-93.7000)


def test_pickup_budget_is_round_trip_not_one_way() -> None:
    """45 minutes at 35mph is ~26 round-trip miles, so ~13 miles each way."""
    availability = Availability(max_pickup_minutes=45, average_speed_mph=35)
    assert availability.max_round_trip_miles() == pytest.approx(26.25)
    assert availability.max_one_way_miles() == pytest.approx(13.125)


def test_item_inside_the_budget_is_a_quick_grab() -> None:
    listing = normalize_ebay_item(make_item("a", "10", 41.60, -93.65))
    tier, base, miles = classify_pickup(
        listing,
        bases=[HOME, WORK],
        availability=Availability(),
        on_planned_route=False,
    )
    assert tier == "quick"
    assert base in {"home", "work"}
    assert miles is not None and miles < 14


def test_item_outside_the_budget_but_on_route_is_on_route() -> None:
    listing = normalize_ebay_item(make_item("b", "10", 42.0653, -94.8669))
    tier, _base, _miles = classify_pickup(
        listing,
        bases=[HOME],
        availability=Availability(),
        on_planned_route=True,
    )
    assert tier == "on_route"


def test_item_that_is_neither_needs_a_special_trip() -> None:
    listing = normalize_ebay_item(make_item("c", "10", 41.9779, -91.6656))
    tier, _base, miles = classify_pickup(
        listing,
        bases=[HOME],
        availability=Availability(),
        on_planned_route=False,
    )
    assert tier == "special_trip"
    assert miles is not None and miles > 100


def test_a_tighter_schedule_shrinks_what_counts_as_quick() -> None:
    """Someone with 20 spare minutes cannot reach what a 90-minute budget can."""
    listing = normalize_ebay_item(make_item("d", "10", 41.75, -93.62))
    generous = Availability(max_pickup_minutes=90, average_speed_mph=40)
    tight = Availability(max_pickup_minutes=20, average_speed_mph=30)

    assert (
        classify_pickup(
            listing, bases=[HOME], availability=generous, on_planned_route=False
        )[0]
        == "quick"
    )
    assert (
        classify_pickup(
            listing, bases=[HOME], availability=tight, on_planned_route=False
        )[0]
        == "special_trip"
    )


def test_no_bases_configured_falls_back_to_route_logic() -> None:
    listing = normalize_ebay_item(make_item("e", "10", 41.60, -93.65))
    tier, base, miles = classify_pickup(
        listing,
        bases=[],
        availability=Availability(),
        on_planned_route=True,
    )
    assert tier == "on_route"
    assert base is None and miles is None


def test_listing_without_coordinates_is_not_called_quick() -> None:
    """Never promise a quick grab for something we cannot locate."""
    listing = normalize_ebay_item(make_item("f", "10", None, None))
    tier, _base, _miles = classify_pickup(
        listing,
        bases=[HOME],
        availability=Availability(),
        on_planned_route=False,
    )
    assert tier == "special_trip"


def test_window_end_must_follow_start() -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        PickupWindow(day="sat", start="13:00", end="09:00")


@pytest.mark.parametrize("day", ["funday", "Monday", ""])
def test_invalid_day_is_rejected(day) -> None:
    with pytest.raises(ValueError):
        PickupWindow(day=day, start="09:00", end="10:00")


def test_weekly_minutes_sums_every_window() -> None:
    availability = Availability(
        windows=[
            PickupWindow(day="sat", start="09:00", end="13:00"),  # 240
            PickupWindow(day="wed", start="17:30", end="20:00"),  # 150
        ]
    )
    assert availability.weekly_minutes() == 390


def test_quick_grabs_outrank_better_far_away_finds(tmp_path: Path) -> None:
    """The core of the time constraint: reachable beats theoretically better."""
    near = make_item("v1|near|0", "200", 41.60, -93.65)  # ~2mi from home
    far = make_item("v1|far|0", "60", 42.0653, -94.8669)  # on route, cheaper

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(200, json={"itemSummaries": [far, near]})

    watchlist = Watchlist(
        routes=[CORRIDOR],
        bases=[HOME],
        availability=Availability(max_pickup_minutes=45),
        searches=[base_search()],
    )
    with Storage(tmp_path / "s.db") as storage:
        result = scan(make_client(handler), watchlist, storage)

    assert result.candidates[0].pickup_tier == "quick"
    assert result.candidates[0].listing.source_item_id == "v1|near|0"


def test_report_groups_by_how_you_would_collect_it(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={
                "itemSummaries": [
                    make_item("v1|near|0", "100", 41.60, -93.65),
                    make_item("v1|route|0", "100", 42.0653, -94.8669),
                ]
            },
        )

    watchlist = Watchlist(
        routes=[CORRIDOR],
        bases=[HOME],
        availability=Availability(max_pickup_minutes=45),
        searches=[base_search()],
    )
    with Storage(tmp_path / "s.db") as storage:
        report = format_report(scan(make_client(handler), watchlist, storage))

    assert "QUICK GRABS" in report
    assert "ON ROUTE" in report
    assert report.index("QUICK GRABS") < report.index("ON ROUTE")


def test_quick_grab_does_not_also_claim_to_be_on_a_route(tmp_path: Path) -> None:
    """A quick errand from home is not "a trip you already make" - pick one."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200, json={"itemSummaries": [make_item("v1|n|0", "100", 41.60, -93.65)]}
        )

    watchlist = Watchlist(
        routes=[CORRIDOR],
        bases=[HOME],
        availability=Availability(max_pickup_minutes=45),
        searches=[base_search()],
    )
    with Storage(tmp_path / "s.db") as storage:
        result = scan(make_client(handler), watchlist, storage)

    reasons = result.candidates[0].reasons
    assert any("quick grab from home" in r for r in reasons)
    assert "on a trip you already make" not in reasons


# --------------------------------------------------------------------------- #
# Technician edge: repair economics, helpers, and work territory
# --------------------------------------------------------------------------- #

from app.services.geo import Helper, ServiceArea  # noqa: E402
from app.services.scanner import PICKUP_TIER_RANK  # noqa: E402

AMES_HELPER = Helper(
    name="coworker-ames", lat=42.0308, lon=-93.6319, max_detour_miles=15, favor_cost=15
)
CENTRAL_IOWA = ServiceArea(
    name="central-iowa",
    center=Waypoint(name="Des Moines", lat=41.5868, lon=-93.6250),
    radius_miles=100,
)


def test_service_area_covers_ames_but_not_cedar_rapids() -> None:
    """100 miles from Des Moines reaches Ames (~31mi), not Cedar Rapids (~105)."""
    assert CENTRAL_IOWA.contains((42.0308, -93.6319)) is True
    assert CENTRAL_IOWA.contains((41.9779, -91.6656)) is False


def test_helper_collects_only_within_their_own_tolerance() -> None:
    assert AMES_HELPER.can_collect((42.05, -93.65)) is True
    assert AMES_HELPER.can_collect((41.5868, -93.6250)) is False


def test_helper_tier_outranks_a_route_detour() -> None:
    """No driving at all beats a short drive."""
    listing = normalize_ebay_item(make_item("a", "50", 42.0308, -93.6319))
    tier, name, _miles = classify_pickup(
        listing,
        bases=[],
        availability=Availability(max_pickup_minutes=10),
        on_planned_route=True,
        helpers=[AMES_HELPER],
    )
    assert tier == "helper"
    assert name == "coworker-ames"


def test_territory_tier_catches_what_routes_miss() -> None:
    """Marshalltown is nowhere near the Sioux City run but is in territory."""
    listing = normalize_ebay_item(make_item("b", "50", 42.0494, -92.9080))
    tier, name, _miles = classify_pickup(
        listing,
        bases=[],
        availability=Availability(max_pickup_minutes=10),
        on_planned_route=False,
        service_areas=[CENTRAL_IOWA],
    )
    assert tier == "in_territory"
    assert name == "central-iowa"


def test_outside_territory_still_needs_a_special_trip() -> None:
    listing = normalize_ebay_item(make_item("c", "50", 41.9779, -91.6656))
    tier, _n, _m = classify_pickup(
        listing,
        bases=[],
        availability=Availability(max_pickup_minutes=10),
        on_planned_route=False,
        service_areas=[CENTRAL_IOWA],
    )
    assert tier == "special_trip"


def test_tier_precedence_is_quick_helper_route_territory() -> None:
    assert (
        PICKUP_TIER_RANK["quick"]
        < PICKUP_TIER_RANK["helper"]
        < PICKUP_TIER_RANK["on_route"]
        < PICKUP_TIER_RANK["in_territory"]
        < PICKUP_TIER_RANK["special_trip"]
    )


def test_repair_odds_reduce_effective_resale() -> None:
    """At 50% revival odds a $400 unit is worth $200 in expectation."""
    listing = normalize_ebay_item(make_item("a", "40", 41.60, -93.65))
    certain = base_search(assumed_resale_price=Decimal("400"))
    risky = base_search(
        assumed_resale_price=Decimal("400"),
        repairable=True,
        repair_success_rate=Decimal("0.5"),
        estimated_repair_cost=Decimal("0"),
    )
    est_certain, _s1, _c1, _r1 = score_candidate(
        listing,
        certain,
        detour_miles=1.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    est_risky, _s2, _c2, reasons = score_candidate(
        listing,
        risky,
        detour_miles=1.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    assert est_certain is not None and est_risky is not None
    assert est_risky.resale_price == Decimal("200.00")
    assert est_risky.net_profit < est_certain.net_profit
    assert any("50% revival odds" in r for r in reasons)


def test_repair_cost_is_charged_against_the_deal() -> None:
    listing = normalize_ebay_item(make_item("a", "40", 41.60, -93.65))
    search = base_search(
        assumed_resale_price=Decimal("400"),
        repairable=True,
        repair_success_rate=Decimal("1"),
        estimated_repair_cost=Decimal("75"),
    )
    estimate, _s, _c, _r = score_candidate(
        listing,
        search,
        detour_miles=1.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    assert estimate is not None
    assert estimate.total_other_costs >= Decimal("75")


def test_a_bad_repair_rate_can_disqualify_an_otherwise_good_multiple() -> None:
    """$40 into a $400 unit is 10x - but not at 10% revival odds."""
    listing = normalize_ebay_item(make_item("a", "40", 41.60, -93.65))
    search = base_search(
        assumed_resale_price=Decimal("400"),
        min_multiple=Decimal("3"),
        repairable=True,
        repair_success_rate=Decimal("0.1"),
        estimated_repair_cost=Decimal("50"),
    )
    _est, score, _c, reasons = score_candidate(
        listing,
        search,
        detour_miles=1.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    assert score == 0
    assert any("below min_multiple" in r for r in reasons)


def test_non_repairable_search_is_unaffected_by_repair_fields() -> None:
    listing = normalize_ebay_item(make_item("a", "40", 41.60, -93.65))
    estimate, _s, _c, reasons = score_candidate(
        listing,
        base_search(assumed_resale_price=Decimal("400")),
        detour_miles=1.0,
        economics=TripEconomics(),
        on_planned_route=True,
    )
    assert estimate is not None
    assert estimate.resale_price == Decimal("400.00")
    assert not any("revival odds" in r for r in reasons)


@pytest.mark.parametrize("rate", [Decimal("0"), Decimal("1.5"), Decimal("-0.2")])
def test_impossible_repair_rates_are_rejected(rate) -> None:
    with pytest.raises(ValueError):
        base_search(repairable=True, repair_success_rate=rate)


def test_helper_pickup_costs_a_favour_not_mileage(tmp_path: Path) -> None:
    """Asking a coworker costs goodwill; it must not be free, nor be fuel."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={"itemSummaries": [make_item("v1|a|0", "50", 42.0308, -93.6319)]},
        )

    watchlist = Watchlist(
        routes=[CORRIDOR],
        helpers=[AMES_HELPER],
        availability=Availability(max_pickup_minutes=10),
        searches=[base_search()],
    )
    with Storage(tmp_path / "s.db") as storage:
        result = scan(make_client(handler), watchlist, storage)

    candidate = result.candidates[0]
    assert candidate.pickup_tier == "helper"
    assert candidate.drive_cost == Decimal("15.00")
    assert any("coworker can collect" in r for r in candidate.reasons)


def test_report_shows_the_coworker_section(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response()
        return httpx.Response(
            200,
            json={"itemSummaries": [make_item("v1|a|0", "50", 42.0308, -93.6319)]},
        )

    watchlist = Watchlist(
        routes=[CORRIDOR],
        helpers=[AMES_HELPER],
        availability=Availability(max_pickup_minutes=10),
        searches=[base_search()],
    )
    with Storage(tmp_path / "s.db") as storage:
        report = format_report(scan(make_client(handler), watchlist, storage))
    assert "ASK A COWORKER" in report

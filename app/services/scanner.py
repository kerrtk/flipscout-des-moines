"""Daily scan: run the watchlist, filter by route, dedupe, rank.

This is orchestration. Every piece of judgement it applies lives in a module
it calls - transport, normalization, geography, arithmetic - so adding a
second marketplace means adding a client and a normalizer, not rewriting this.

The economic idea it encodes: **an item's cost includes the drive.** Two
identical listings at the same price are not the same deal if one is on the
Sioux City run and the other is 70 miles the wrong way. A detour on a trip
you were already making costs only marginal fuel; a special trip costs the
whole round trip plus a day. The scanner charges each accordingly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models import NormalizedListing, ProfitAssumptions, ProfitEstimate
from app.services.ebay_client import EbayClient, EbayError, InvalidSearchRequest
from app.services.geo import Base, Route, Waypoint, nearest_base
from app.services.normalization import normalize_ebay_search_response
from app.services.profitability import estimate_profit
from app.services.watchlist import Availability, SavedSearch, Watchlist
from app.storage import Storage

logger = logging.getLogger(__name__)


class TripEconomics(BaseModel):
    """What driving actually costs you.

    Defaults are box-truck shaped: a loaded box truck is nowhere near a
    sedan's mileage, and pretending otherwise is how a "profitable" 90-mile
    round trip quietly loses money.
    """

    model_config = ConfigDict(extra="forbid")

    miles_per_gallon: Decimal = Field(default=Decimal("10"), gt=0)
    fuel_price_per_gallon: Decimal = Field(default=Decimal("3.20"), ge=0)
    #: Charged only for a dedicated trip, not for an on-route detour.
    hourly_value_of_time: Decimal = Field(default=Decimal("25"), ge=0)
    average_speed_mph: Decimal = Field(default=Decimal("55"), gt=0)

    def fuel_cost_for(self, miles: Decimal) -> Decimal:
        return (miles / self.miles_per_gallon) * self.fuel_price_per_gallon

    def time_cost_for(self, miles: Decimal) -> Decimal:
        return (miles / self.average_speed_mph) * self.hourly_value_of_time


class Candidate(BaseModel):
    """One listing, scored and located relative to your routes."""

    model_config = ConfigDict(extra="forbid")

    listing: NormalizedListing
    search_name: str
    route_name: str | None = None
    nearest_stop: str | None = None
    detour_miles: float | None = None
    round_trip_miles: float | None = None
    drive_cost: Decimal | None = None
    estimate: ProfitEstimate | None = None
    score: Decimal = Decimal("0")
    reasons: list[str] = Field(default_factory=list)

    #: How you would actually collect this. See ``classify_pickup``.
    pickup_tier: str = "special_trip"
    base_name: str | None = None
    base_miles: float | None = None


@dataclass
class ScanResult:
    """Everything one pass produced, including what it could not do."""

    candidates: list[Candidate] = field(default_factory=list)
    searches_run: int = 0
    api_calls: int = 0
    items_seen: int = 0
    items_new: int = 0
    errors: list[str] = field(default_factory=list)


def _listing_point(listing: NormalizedListing) -> tuple[float, float] | None:
    """Coordinates for a listing, when eBay gave us any."""
    if listing.latitude is not None and listing.longitude is not None:
        return (listing.latitude, listing.longitude)
    return None


def locate(
    listing: NormalizedListing, routes: list[Route]
) -> tuple[Route | None, Waypoint | None, float | None]:
    """Find the route this listing is closest to.

    Returns ``(route, nearest_stop, detour_miles)``. All three are ``None``
    when the listing carries no coordinates - eBay frequently omits them, and
    guessing a location from a postal code we have not geocoded would be
    worse than admitting we do not know.
    """
    point = _listing_point(listing)
    if point is None or not routes:
        return (None, None, None)

    best_route: Route | None = None
    best_detour = float("inf")
    for route in routes:
        detour = route.detour_miles(point)
        if detour < best_detour:
            best_route, best_detour = route, detour

    if best_route is None:
        return (None, None, None)
    return (best_route, best_route.nearest_waypoint(point), best_detour)


#: Ordering for the report. A thing you can grab on a lunch break outranks a
#: better thing that needs a Saturday, because the Saturday one competes with
#: everything else you could do with a Saturday.
PICKUP_TIER_RANK = {"quick": 0, "on_route": 1, "special_trip": 2}


def classify_pickup(
    listing: NormalizedListing,
    *,
    bases: list[Base],
    availability: Availability,
    on_planned_route: bool,
) -> tuple[str, str | None, float | None]:
    """Decide how a find would actually be collected.

    Returns ``(tier, base_name, miles_from_base)``.

    - ``quick``        - inside the round-trip budget from a base you already
                         sit at during the week. Grabbable on a lunch break.
    - ``on_route``     - a detour on a drive you already make.
    - ``special_trip`` - neither. It has to be worth burning a Saturday.

    The distinction matters when time, not distance, is the binding
    constraint: an hour you do not have is infinitely expensive.
    """
    point = _listing_point(listing)
    base, miles = (None, None)
    if point is not None:
        base, miles = nearest_base(point, bases)

    if miles is not None and miles <= availability.max_one_way_miles():
        return ("quick", base.name if base else None, round(miles, 1))
    if on_planned_route:
        return (
            "on_route",
            base.name if base else None,
            round(miles, 1) if miles is not None else None,
        )
    return (
        "special_trip",
        base.name if base else None,
        round(miles, 1) if miles is not None else None,
    )


def score_candidate(
    listing: NormalizedListing,
    search: SavedSearch,
    *,
    detour_miles: float | None,
    economics: TripEconomics,
    on_planned_route: bool,
) -> tuple[ProfitEstimate | None, Decimal, Decimal | None, list[str]]:
    """Price the drive into the deal and produce a ranking score.

    Returns ``(estimate, score, drive_cost, reasons)``.

    The resale price used here is ``search.assumed_resale_price`` - a number
    YOU supplied. It is not evidence. Until sold-comps data is connected, a
    high score means "worth a human look", never "this is worth that much".
    """
    reasons: list[str] = []

    if listing.price_value is None or listing.price_value <= 0:
        return (None, Decimal("0"), None, ["no usable asking price"])

    if search.assumed_resale_price is None:
        reasons.append("no resale estimate set - score is placement only")
        return (None, Decimal("0"), None, reasons)

    # Round trip: you have to come back. An on-route detour is charged as
    # marginal fuel only; a dedicated trip also costs your time.
    round_trip = Decimal(str((detour_miles or 0.0) * 2))
    drive_cost = economics.fuel_cost_for(round_trip)
    if not on_planned_route:
        drive_cost += economics.time_cost_for(round_trip)
        reasons.append("dedicated trip - time charged")
    else:
        reasons.append("on a trip you already make")

    assumptions = ProfitAssumptions(
        resale_price=search.assumed_resale_price,
        purchase_price=listing.price_value,
        shipping_cost=listing.shipping_cost or Decimal("0"),
        fuel_cost=drive_cost.quantize(Decimal("0.01")),
    )
    estimate = estimate_profit(assumptions)

    if estimate.gross_multiple < search.min_multiple:
        reasons.append(
            f"below min_multiple ({estimate.gross_multiple} < {search.min_multiple})"
        )
        return (estimate, Decimal("0"), drive_cost, reasons)

    # Rank by net profit per mile driven, floored so a zero-detour item does
    # not divide by zero. Profit alone would always favour the far-away item.
    miles_floor = max(round_trip, Decimal("1"))
    score = (estimate.net_profit / miles_floor).quantize(Decimal("0.0001"))
    if estimate.qualifies_for_500_percent_resale_multiple:
        reasons.append("clears the 5x resale multiple")
    return (estimate, score, drive_cost, reasons)


def scan(
    client: EbayClient,
    watchlist: Watchlist,
    storage: Storage,
    *,
    economics: TripEconomics | None = None,
    include_seen: bool = False,
    limit_per_search: int | None = None,
) -> ScanResult:
    """Run every enabled search and return ranked candidates.

    ``include_seen=False`` (the default) is what makes a *daily* report
    readable: only listings never surfaced before appear.
    """
    economics = economics or TripEconomics()
    result = ScanResult()
    rejected = storage.rejected_ids("ebay")

    for search in watchlist.active_searches():
        routes = watchlist.routes_for(search)
        # A local-pickup search must be fanned out over the corridor's
        # anchors, because eBay's pickup filter is a single circle.
        anchors: list[str | None]
        if search.local_pickup_only:
            anchors = [
                code for route in routes for code in route.anchor_postal_codes()
            ] or [None]
        else:
            anchors = [None]

        for anchor in anchors:
            try:
                payload = client.search(
                    keyword=search.q,
                    limit=limit_per_search or search.limit,
                    condition=search.condition.value if search.condition else None,
                    max_price=search.max_price,
                    local_pickup_only=bool(anchor),
                    pickup_postal_code=anchor,
                    pickup_radius_miles=int(
                        min(r.max_detour_miles for r in routes) if routes else 25
                    ),
                )
                result.api_calls += 1
            except (EbayError, InvalidSearchRequest) as exc:
                # One bad anchor must not abort the whole morning's scan.
                message = f"{search.name}@{anchor or 'any'}: {exc}"
                logger.warning("Search failed: %s", message)
                result.errors.append(message)
                continue

            listings = normalize_ebay_search_response(payload)
            result.items_seen += len(listings)

            for listing in listings:
                item_id = listing.source_item_id
                if not item_id or item_id in rejected:
                    continue
                already_seen = storage.has_seen("ebay", item_id)
                storage.record_seen(
                    source="ebay",
                    source_item_id=item_id,
                    title=listing.title,
                    url=listing.url,
                    price_value=listing.price_value,
                    price_currency=listing.price_currency,
                    postal_code=listing.postal_code,
                    location_text=listing.location_text,
                    search_name=search.name,
                )
                if already_seen and not include_seen:
                    continue
                result.items_new += 1

                route, stop, detour = locate(listing, routes)
                on_route = (
                    route is not None
                    and detour is not None
                    and detour <= route.max_detour_miles
                )
                tier, base_name, base_miles = classify_pickup(
                    listing,
                    bases=watchlist.bases,
                    availability=watchlist.availability,
                    on_planned_route=on_route,
                )
                # A quick grab from a base is charged as an on-route errand:
                # it fits in time you are already spending near that base.
                estimate, score, drive_cost, reasons = score_candidate(
                    listing,
                    search,
                    detour_miles=(
                        base_miles
                        if tier == "quick" and base_miles is not None
                        else detour
                    ),
                    economics=economics,
                    on_planned_route=on_route or tier == "quick",
                )
                if score <= 0:
                    continue
                if tier == "quick":
                    # Replace the route wording: a quick grab is an errand from
                    # a base, not a detour on a trip already being made.
                    reasons = [r for r in reasons if r != "on a trip you already make"]
                    reasons.insert(0, f"quick grab from {base_name}")

                result.candidates.append(
                    Candidate(
                        listing=listing,
                        search_name=search.name,
                        route_name=route.name if route else None,
                        nearest_stop=stop.name if stop else None,
                        detour_miles=round(detour, 1) if detour is not None else None,
                        round_trip_miles=(
                            round(detour * 2, 1) if detour is not None else None
                        ),
                        drive_cost=(
                            drive_cost.quantize(Decimal("0.01"))
                            if drive_cost is not None
                            else None
                        ),
                        estimate=estimate,
                        score=score,
                        reasons=reasons,
                        pickup_tier=tier,
                        base_name=base_name,
                        base_miles=base_miles,
                    )
                )

        result.searches_run += 1

    # Tier first, then score: the best thing you cannot go get is worth less
    # than a decent thing you can.
    result.candidates.sort(
        key=lambda c: (PICKUP_TIER_RANK.get(c.pickup_tier, 9), -c.score)
    )
    return result


def format_report(result: ScanResult, *, top: int = 20) -> str:
    """Render a scan as plain text for an email or a terminal."""
    lines: list[str] = []
    lines.append(
        f"FlipScout scan: {result.searches_run} searches, "
        f"{result.api_calls} API calls, {result.items_seen} listings, "
        f"{result.items_new} new, {len(result.candidates)} candidates"
    )
    lines.append("")

    if not result.candidates:
        lines.append("No candidates cleared their thresholds.")

    headings = {
        "quick": "QUICK GRABS - reachable in your pickup window",
        "on_route": "ON ROUTE - collect on a drive you already make",
        "special_trip": "WORTH A SPECIAL TRIP - only if the margin justifies a day",
    }
    current_tier: str | None = None

    for index, candidate in enumerate(result.candidates[:top], start=1):
        listing = candidate.listing
        if candidate.pickup_tier != current_tier:
            current_tier = candidate.pickup_tier
            lines.append(headings.get(current_tier, current_tier).upper())
            lines.append("-" * 62)
        lines.append(f"{index}. {listing.title or '(untitled)'}")
        lines.append(
            f"   ask ${listing.price_value}  "
            f"net ${candidate.estimate.net_profit if candidate.estimate else '?'}  "
            f"x{candidate.estimate.gross_multiple if candidate.estimate else '?'}  "
            f"score {candidate.score}/mi"
        )
        where = candidate.nearest_stop or listing.location_text or "location unknown"
        if candidate.pickup_tier == "quick" and candidate.base_miles is not None:
            placement = f"{candidate.base_miles}mi from {candidate.base_name}"
        elif candidate.detour_miles is not None:
            placement = f"{candidate.detour_miles}mi off {candidate.route_name}"
        else:
            placement = "off-route distance unknown"
        lines.append(f"   {where} - {placement} - drive ${candidate.drive_cost or 0}")
        if candidate.reasons:
            lines.append(f"   {'; '.join(candidate.reasons)}")
        if listing.url:
            lines.append(f"   {listing.url}")
        lines.append("")

    if result.errors:
        lines.append(f"{len(result.errors)} search error(s):")
        lines.extend(f"  - {e}" for e in result.errors)

    lines.append("")
    lines.append(
        "REMINDER: prices above are ASKING prices from active listings, and "
        "resale figures come from assumed_resale_price in your watchlist. "
        "Neither is sold-comp evidence. Verify before buying."
    )
    return "\n".join(lines)

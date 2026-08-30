"""Command-line entrypoint for the daily scan.

This is what cron runs. Everything it needs comes from the watchlist file and
the environment, so a scheduled run has no hidden arguments.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.config import MissingConfigurationError, get_settings
from app.services.ebay_client import EbayClient
from app.services.geo import total_route_miles
from app.services.scanner import TripEconomics, format_report, scan
from app.services.watchlist import WatchlistError, load_watchlist
from app.storage import Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flipscout",
        description="Scan saved searches for resale candidates along your routes.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_cmd = sub.add_parser("scan", help="Run every enabled saved search.")
    scan_cmd.add_argument("--watchlist", default="watchlist.yaml", type=Path)
    scan_cmd.add_argument("--db", default="flipscout.db", type=Path)
    scan_cmd.add_argument("--top", type=int, default=20, help="Rows to print.")
    scan_cmd.add_argument(
        "--include-seen",
        action="store_true",
        help="Include listings surfaced on a previous run.",
    )
    scan_cmd.add_argument("--mpg", type=float, default=10.0)
    scan_cmd.add_argument("--fuel", type=float, default=3.20)
    scan_cmd.add_argument("--verbose", action="store_true")

    check = sub.add_parser("check", help="Validate the watchlist without calling eBay.")
    check.add_argument("--watchlist", default="watchlist.yaml", type=Path)

    stats = sub.add_parser("stats", help="Show database counts and calibration.")
    stats.add_argument("--db", default="flipscout.db", type=Path)

    return parser


def cmd_check(args: argparse.Namespace) -> int:
    """Parse and validate the watchlist. Makes no network call."""
    watchlist = load_watchlist(args.watchlist)
    print(f"{args.watchlist}: OK")
    print(f"\nRoutes ({len(watchlist.routes)}):")
    for route in watchlist.routes:
        miles = total_route_miles(route.waypoints)
        print(
            f"  {route.name}: {len(route.waypoints)} stops, ~{miles:.0f} mi, "
            f"detour tolerance {route.max_detour_miles} mi"
        )
    active = watchlist.active_searches()
    print(f"\nSearches ({len(active)} enabled of {len(watchlist.searches)}):")
    for search in active:
        scope = ", ".join(search.routes) or "all routes"
        pickup = " [local pickup]" if search.local_pickup_only else ""
        resale = (
            f"resale ${search.assumed_resale_price}"
            if search.assumed_resale_price
            else "NO RESALE ESTIMATE - will not score"
        )
        print(f"  {search.name}: {search.q!r}{pickup}")
        print(f"      {scope} | min x{search.min_multiple} | {resale}")
    missing = [s.name for s in active if s.assumed_resale_price is None]
    if missing:
        print(
            f"\n{len(missing)} search(es) have no assumed_resale_price and will "
            f"be skipped when scoring: {', '.join(missing)}"
        )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with Storage(args.db) as storage:
        print(f"Database: {args.db}")
        for key, value in storage.stats().items():
            print(f"  {key}: {value}")
        report = storage.calibration_report()
        print("\nCalibration (actual vs predicted resale):")
        print(f"  samples: {report['samples']}")
        if report["median_ratio"] is not None:
            print(f"  median ratio: {report['median_ratio']:.3f}")
        print(f"  {report['advice']}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    watchlist = load_watchlist(args.watchlist)
    economics = TripEconomics(
        miles_per_gallon=str(args.mpg), fuel_price_per_gallon=str(args.fuel)
    )
    client = EbayClient(get_settings())
    try:
        with Storage(args.db) as storage:
            result = scan(
                client,
                watchlist,
                storage,
                economics=economics,
                include_seen=args.include_seen,
            )
    finally:
        client.close()

    print(format_report(result, top=args.top))
    # Non-zero only when every search failed - a partial failure still has
    # useful output, and a cron job that always alerts gets ignored.
    return 1 if result.errors and result.searches_run == 0 else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            return cmd_scan(args)
        if args.command == "check":
            return cmd_check(args)
        if args.command == "stats":
            return cmd_stats(args)
    except WatchlistError as exc:
        print(f"Watchlist error: {exc}", file=sys.stderr)
        return 2
    except MissingConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

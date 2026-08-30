"""Dedup and calibration - the memory that makes a daily loop usable."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    with Storage(tmp_path / "test.db") as store:
        yield store


def test_unseen_item_is_not_reported_as_seen(storage: Storage) -> None:
    assert storage.has_seen("ebay", "v1|1|0") is False


def test_recording_makes_an_item_seen(storage: Storage) -> None:
    storage.record_seen(source="ebay", source_item_id="v1|1|0", title="Saw")
    assert storage.has_seen("ebay", "v1|1|0") is True


def test_reseeing_bumps_the_counter_without_duplicating(storage: Storage) -> None:
    """Yesterday's listing must not create a second row today."""
    for _ in range(3):
        storage.record_seen(source="ebay", source_item_id="v1|1|0", title="Saw")
    assert storage.stats()["seen_items"] == 1


def test_filter_unseen_returns_only_new_ids(storage: Storage) -> None:
    storage.record_seen(source="ebay", source_item_id="known")
    assert storage.filter_unseen("ebay", ["known", "new-a", "new-b"]) == {
        "new-a",
        "new-b",
    }


def test_filter_unseen_handles_an_empty_input(storage: Storage) -> None:
    assert storage.filter_unseen("ebay", []) == set()


def test_sources_are_isolated_from_each_other(storage: Storage) -> None:
    """A facebook id must not shadow an identical ebay id."""
    storage.record_seen(source="ebay", source_item_id="same-id")
    assert storage.has_seen("facebook", "same-id") is False


def test_money_round_trips_exactly_as_decimal(storage: Storage) -> None:
    """Stored as TEXT precisely so 24.99 comes back as 24.99."""
    storage.record_seen(
        source="ebay", source_item_id="v1|1|0", price_value=Decimal("24.99")
    )
    row = storage._connection.execute(
        "SELECT price_value FROM seen_items WHERE source_item_id = 'v1|1|0'"
    ).fetchone()
    assert Decimal(row["price_value"]) == Decimal("24.99")


def test_rejected_items_are_recoverable(storage: Storage) -> None:
    storage.set_verdict("ebay", "junk", "reject", "cracked")
    assert storage.rejected_ids("ebay") == {"junk"}


def test_verdict_can_be_changed(storage: Storage) -> None:
    storage.set_verdict("ebay", "x", "reject")
    storage.set_verdict("ebay", "x", "pursue")
    assert storage.rejected_ids("ebay") == set()


def test_invalid_verdict_is_rejected_by_the_schema(storage: Storage) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        storage.set_verdict("ebay", "x", "maybe-someday")


def test_calibration_reports_nothing_without_sales(storage: Storage) -> None:
    report = storage.calibration_report()
    assert report["samples"] == 0
    assert report["median_ratio"] is None


def test_calibration_detects_an_optimistic_estimator(storage: Storage) -> None:
    """Predicting $200 and getting $150 must show as a 0.75 ratio."""
    storage.record_outcome(
        source="ebay",
        purchase_price=Decimal("40"),
        predicted_resale=Decimal("200"),
        actual_resale=Decimal("150"),
    )
    report = storage.calibration_report()
    assert report["samples"] == 1
    assert report["median_ratio"] == Decimal("0.75")
    assert "optimistic" in report["advice"]


def test_calibration_detects_a_conservative_estimator(storage: Storage) -> None:
    storage.record_outcome(
        source="ebay",
        purchase_price=Decimal("10"),
        predicted_resale=Decimal("100"),
        actual_resale=Decimal("130"),
    )
    assert "conservative" in storage.calibration_report()["advice"]


def test_calibration_ignores_open_positions(storage: Storage) -> None:
    """Something bought but not yet sold has no ratio to contribute."""
    storage.record_outcome(
        source="ebay", purchase_price=Decimal("40"), predicted_resale=Decimal("200")
    )
    assert storage.calibration_report()["samples"] == 0


def test_database_persists_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    with Storage(path) as first:
        first.record_seen(source="ebay", source_item_id="v1|1|0")
    with Storage(path) as second:
        assert second.has_seen("ebay", "v1|1|0") is True


# --------------------------------------------------------------------------- #
# Closing outcomes - the loop that turns guesses into calibration
# --------------------------------------------------------------------------- #


def test_open_purchase_is_listed_until_it_sells(storage: Storage) -> None:
    outcome_id = storage.record_outcome(
        source="ebay",
        title="ReBuilder 2407",
        purchase_price=Decimal("10"),
        predicted_resale=Decimal("400"),
    )
    assert len(storage.list_outcomes(open_only=True)) == 1
    storage.close_outcome(outcome_id, actual_resale=Decimal("285"))
    assert storage.list_outcomes(open_only=True) == []


def test_closing_records_the_real_price(storage: Storage) -> None:
    outcome_id = storage.record_outcome(
        source="ebay",
        purchase_price=Decimal("10"),
        predicted_resale=Decimal("400"),
    )
    assert (
        storage.close_outcome(
            outcome_id, actual_resale=Decimal("285"), total_fees=Decimal("38")
        )
        is True
    )
    row = storage.list_outcomes()[0]
    assert Decimal(row["actual_resale"]) == Decimal("285.00")
    assert Decimal(row["total_fees"]) == Decimal("38.00")
    assert row["sold_at"] is not None


def test_closing_an_unknown_id_reports_failure(storage: Storage) -> None:
    assert storage.close_outcome(999, actual_resale=Decimal("10")) is False


def test_a_single_sale_immediately_calibrates(storage: Storage) -> None:
    """Predicting $400 and getting $285 is a 0.7125 ratio - haircut future ones."""
    outcome_id = storage.record_outcome(
        source="ebay",
        purchase_price=Decimal("10"),
        predicted_resale=Decimal("400"),
    )
    storage.close_outcome(outcome_id, actual_resale=Decimal("285"))
    report = storage.calibration_report()
    assert report["samples"] == 1
    assert report["median_ratio"] == Decimal("0.7125")
    assert "optimistic" in report["advice"]


def test_closing_preserves_fields_not_supplied(storage: Storage) -> None:
    """A sell that omits fees must not wipe what was already recorded."""
    outcome_id = storage.record_outcome(
        source="ebay",
        purchase_price=Decimal("10"),
        total_fees=Decimal("5"),
        notes="original note",
    )
    storage.close_outcome(outcome_id, actual_resale=Decimal("100"))
    row = storage.list_outcomes()[0]
    assert Decimal(row["total_fees"]) == Decimal("5")
    assert row["notes"] == "original note"


def test_list_outcomes_is_newest_first(storage: Storage) -> None:
    first = storage.record_outcome(source="ebay", purchase_price=Decimal("1"))
    second = storage.record_outcome(source="ebay", purchase_price=Decimal("2"))
    assert [r["id"] for r in storage.list_outcomes()] == [second, first]

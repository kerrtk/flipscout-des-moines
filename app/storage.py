"""SQLite persistence for the daily scan.

Why a database now, having avoided one: a *daily* loop is meaningless without
memory. Without dedup, every morning re-surfaces yesterday's rejects and the
report becomes noise you stop reading. Without outcomes, the estimator never
learns it is wrong.

SQLite specifically: stdlib, no server, one file you can copy or delete.
Nothing here needs Postgres.

Money is stored as TEXT, not REAL. SQLite's REAL is IEEE-754 binary float,
which is exactly what Decimal exists to avoid - round-tripping 24.99 through
REAL does not reliably give back 24.99.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

DEFAULT_DB_PATH = Path("flipscout.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    source           TEXT NOT NULL,
    source_item_id   TEXT NOT NULL,
    title            TEXT,
    url              TEXT,
    price_value      TEXT,
    price_currency   TEXT,
    postal_code      TEXT,
    location_text    TEXT,
    search_name      TEXT,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    times_seen       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source, source_item_id)
);

CREATE TABLE IF NOT EXISTS verdicts (
    source          TEXT NOT NULL,
    source_item_id  TEXT NOT NULL,
    verdict         TEXT NOT NULL CHECK (verdict IN ('pursue','reject','bought')),
    reason          TEXT,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (source, source_item_id)
);

CREATE TABLE IF NOT EXISTS outcomes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL,
    source_item_id      TEXT,
    title               TEXT,
    purchase_price      TEXT NOT NULL,
    predicted_resale    TEXT,
    actual_resale       TEXT,
    total_fees          TEXT,
    total_other_costs   TEXT,
    bought_at           TEXT,
    sold_at             TEXT,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_seen_last ON seen_items (last_seen_at);
CREATE INDEX IF NOT EXISTS idx_seen_search ON seen_items (search_name);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


class Storage:
    """Thin data-access layer over one SQLite file."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        # WAL keeps a long scan from blocking a concurrent read, and foreign
        # keys are off by default in SQLite - both are one-time pragmas.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._connection
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    # -- seen items ------------------------------------------------------- #

    def has_seen(self, source: str, source_item_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM seen_items WHERE source = ? AND source_item_id = ?",
            (source, source_item_id),
        ).fetchone()
        return row is not None

    def filter_unseen(self, source: str, item_ids: Iterable[str]) -> set[str]:
        """Return the subset of ``item_ids`` not yet recorded.

        One query rather than N, so a 200-item page is a single round trip.
        """
        ids = [i for i in item_ids if i]
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        rows = self._connection.execute(
            f"SELECT source_item_id FROM seen_items "  # noqa: S608 - placeholders only
            f"WHERE source = ? AND source_item_id IN ({placeholders})",
            (source, *ids),
        ).fetchall()
        return set(ids) - {row["source_item_id"] for row in rows}

    def record_seen(
        self,
        *,
        source: str,
        source_item_id: str,
        title: str | None = None,
        url: str | None = None,
        price_value: Decimal | None = None,
        price_currency: str | None = None,
        postal_code: str | None = None,
        location_text: str | None = None,
        search_name: str | None = None,
    ) -> None:
        """Insert, or bump last_seen/times_seen if already known."""
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO seen_items (
                    source, source_item_id, title, url, price_value,
                    price_currency, postal_code, location_text, search_name,
                    first_seen_at, last_seen_at, times_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(source, source_item_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    times_seen   = seen_items.times_seen + 1
                """,
                (
                    source,
                    source_item_id,
                    title,
                    url,
                    _money(price_value),
                    price_currency,
                    postal_code,
                    location_text,
                    search_name,
                    now,
                    now,
                ),
            )

    # -- verdicts --------------------------------------------------------- #

    def set_verdict(
        self, source: str, source_item_id: str, verdict: str, reason: str | None = None
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO verdicts (source, source_item_id, verdict, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, source_item_id) DO UPDATE SET
                    verdict = excluded.verdict,
                    reason = excluded.reason,
                    created_at = excluded.created_at
                """,
                (source, source_item_id, verdict, reason, _now()),
            )

    def rejected_ids(self, source: str) -> set[str]:
        rows = self._connection.execute(
            "SELECT source_item_id FROM verdicts WHERE source = ? AND verdict = 'reject'",
            (source,),
        ).fetchall()
        return {row["source_item_id"] for row in rows}

    # -- outcomes --------------------------------------------------------- #

    def record_outcome(self, **fields: Any) -> int:
        """Log a real purchase or sale. This is what calibrates the estimator."""
        for key in (
            "purchase_price",
            "predicted_resale",
            "actual_resale",
            "total_fees",
            "total_other_costs",
        ):
            if isinstance(fields.get(key), Decimal):
                fields[key] = str(fields[key])
        columns = ", ".join(fields)
        placeholders = ", ".join("?" * len(fields))
        with self._transaction() as connection:
            cursor = connection.execute(
                f"INSERT INTO outcomes ({columns}) VALUES ({placeholders})",  # noqa: S608
                tuple(fields.values()),
            )
        return int(cursor.lastrowid or 0)

    def calibration_report(self) -> dict[str, Any]:
        """Compare predicted resale against what things actually sold for.

        Returns the median ratio actual/predicted. Below 1.0 means the
        estimator is optimistic - multiply future estimates by it.
        """
        rows = self._connection.execute(
            "SELECT predicted_resale, actual_resale FROM outcomes "
            "WHERE predicted_resale IS NOT NULL AND actual_resale IS NOT NULL"
        ).fetchall()
        ratios = []
        for row in rows:
            try:
                predicted = Decimal(row["predicted_resale"])
                actual = Decimal(row["actual_resale"])
            except (TypeError, ArithmeticError):
                continue
            if predicted > 0:
                ratios.append(actual / predicted)
        if not ratios:
            return {"samples": 0, "median_ratio": None, "advice": "No closed sales yet."}
        ratios.sort()
        median = ratios[len(ratios) // 2]
        return {
            "samples": len(ratios),
            "median_ratio": median,
            "advice": (
                "Estimator is optimistic; haircut estimates."
                if median < 1
                else "Estimator is conservative."
            ),
        }

    def stats(self) -> dict[str, int]:
        def count(table: str) -> int:
            return int(
                self._connection.execute(
                    f"SELECT COUNT(*) AS c FROM {table}"  # noqa: S608 - fixed names
                ).fetchone()["c"]
            )

        return {
            "seen_items": count("seen_items"),
            "verdicts": count("verdicts"),
            "outcomes": count("outcomes"),
        }

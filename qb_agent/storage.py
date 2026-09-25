"""A small SQLite file that remembers what has already been sent to QuickBooks.

This is what makes the app safe to re-run: uploading the same spreadsheet twice
does not create the same invoice twice, because every finished row is recorded
here with the QuickBooks IDs it produced.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import WorkRecord


@dataclass(frozen=True, slots=True)
class RecordState:
    source_id: str
    mode: str
    status: str
    customer_list_id: str | None
    job_list_id: str | None
    invoice_txn_id: str | None
    invoice_ref_number: str | None
    error_message: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class InvoiceState:
    """One policy invoice, tracked apart from the customer and from each other.

    A row can have its customer created, its premium invoice raised and its
    commission invoice still outstanding, so each needs its own status.
    """

    kind: str
    status: str
    txn_id: str | None = None
    ref_number: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class Event:
    source_id: str
    label: str
    mode: str
    status: str
    message: str
    created_at: str
    # Kept so a failure can be read back in full long after the toast is gone:
    # which action ran, how far it got, and what QuickBooks actually said.
    operation: str = ""
    steps: tuple[str, ...] = ()
    error: str = ""
    row_number: int = 0


class Store:
    def __init__(self, filename: str | Path):
        self.filename = str(filename)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.filename, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS record_state (
                    source_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    customer_list_id TEXT,
                    job_list_id TEXT,
                    invoice_txn_id TEXT,
                    invoice_ref_number TEXT,
                    error_message TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, mode)
                );

                CREATE TABLE IF NOT EXISTS invoice_state (
                    source_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    txn_id TEXT,
                    ref_number TEXT,
                    error_message TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, mode, kind)
                );

                CREATE TABLE IF NOT EXISTS row_overrides (
                    source_id TEXT NOT NULL,
                    field TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, field)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    operation TEXT,
                    steps_json TEXT,
                    error_message TEXT,
                    row_number INTEGER
                );
                """
            )
            # Databases created before jobs existed are missing this column.
            existing = {row["name"] for row in connection.execute("PRAGMA table_info(record_state)")}
            if "job_list_id" not in existing:
                connection.execute("ALTER TABLE record_state ADD COLUMN job_list_id TEXT")
            # Databases from before the log view lack the detail columns.
            event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
            for column, kind in (
                ("operation", "TEXT"), ("steps_json", "TEXT"),
                ("error_message", "TEXT"), ("row_number", "INTEGER"),
            ):
                if column not in event_columns:
                    connection.execute(f"ALTER TABLE events ADD COLUMN {column} {kind}")

            # Premium invoices were briefly columns on record_state, before
            # commission invoices made a row per kind the better shape.
            if "premium_status" in existing:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO invoice_state (
                        source_id, mode, kind, status, txn_id, ref_number,
                        error_message, updated_at
                    )
                    SELECT source_id, mode, 'premium', premium_status, premium_txn_id,
                           premium_ref_number, premium_error, updated_at
                    FROM record_state
                    WHERE premium_status IS NOT NULL AND premium_status <> ''
                    """
                )

    def get(self, source_id: str, mode: str) -> RecordState | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM record_state WHERE source_id=? AND mode=?", (source_id, mode)
            ).fetchone()
        return _state(row) if row else None

    def save(
        self,
        record: WorkRecord,
        mode: str,
        *,
        status: str,
        message: str,
        customer_list_id: str | None = None,
        job_list_id: str | None = None,
        invoice_txn_id: str | None = None,
        invoice_ref_number: str | None = None,
        error_message: str | None = None,
        operation: str = "",
        steps: list[str] | None = None,
    ) -> RecordState:
        now = _now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO record_state (
                    source_id, mode, status, customer_list_id, job_list_id, invoice_txn_id,
                    invoice_ref_number, error_message, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, mode) DO UPDATE SET
                    status=excluded.status,
                    customer_list_id=COALESCE(excluded.customer_list_id, record_state.customer_list_id),
                    job_list_id=COALESCE(excluded.job_list_id, record_state.job_list_id),
                    invoice_txn_id=COALESCE(excluded.invoice_txn_id, record_state.invoice_txn_id),
                    invoice_ref_number=COALESCE(excluded.invoice_ref_number, record_state.invoice_ref_number),
                    error_message=excluded.error_message,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    record.source_id,
                    mode,
                    status,
                    customer_list_id,
                    job_list_id,
                    invoice_txn_id,
                    invoice_ref_number,
                    error_message,
                    json.dumps(record.to_dict(), sort_keys=True),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO events (
                    source_id, label, mode, status, message, created_at,
                    operation, steps_json, error_message, row_number
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.source_id,
                    _label(record),
                    mode,
                    status,
                    message,
                    now,
                    operation,
                    json.dumps(list(steps or ())),
                    error_message,
                    record.row_number,
                ),
            )
        state = self.get(record.source_id, mode)
        assert state is not None
        return state

    def save_invoice(
        self,
        record: WorkRecord,
        mode: str,
        kind: str,
        *,
        status: str,
        message: str,
        txn_id: str | None = None,
        ref_number: str | None = None,
        error_message: str | None = None,
        steps: list[str] | None = None,
    ) -> None:
        """Record one invoice without touching the customer's own status."""
        now = _now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO invoice_state (
                    source_id, mode, kind, status, txn_id, ref_number,
                    error_message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, mode, kind) DO UPDATE SET
                    status=excluded.status,
                    txn_id=COALESCE(excluded.txn_id, invoice_state.txn_id),
                    ref_number=COALESCE(excluded.ref_number, invoice_state.ref_number),
                    error_message=excluded.error_message,
                    updated_at=excluded.updated_at
                """,
                (record.source_id, mode, kind, status, txn_id, ref_number, error_message, now),
            )
            connection.execute(
                """
                INSERT INTO events (
                    source_id, label, mode, status, message, created_at,
                    operation, steps_json, error_message, row_number
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.source_id,
                    f"{_label(record)} ({kind} invoice)",
                    mode,
                    status,
                    message,
                    now,
                    f"{kind}_invoice",
                    json.dumps(list(steps or ())),
                    error_message,
                    record.row_number,
                ),
            )

    def set_invoice_needed(
        self, source_id: str, mode: str, kind: str, needed: bool
    ) -> None:
        """Mark one kind of invoice as not wanted for one policy, or wanted again.

        Not every policy earns all three invoices - plenty carry no fee, and
        some carriers are not commissioned. Marked rows are held here rather
        than in the sheet, so nothing has to change upstream, and "not needed"
        is kept apart from "not done yet" in every count and filter.
        """
        with self.connection() as connection:
            if needed:
                # Only ever clears the mark: a real invoice is never dropped.
                connection.execute(
                    "DELETE FROM invoice_state WHERE source_id=? AND mode=? "
                    "AND kind=? AND status='not_needed'",
                    (source_id, mode, kind),
                )
                return
            existing = connection.execute(
                "SELECT status, txn_id FROM invoice_state "
                "WHERE source_id=? AND mode=? AND kind=?",
                (source_id, mode, kind),
            ).fetchone()
            if existing is not None and existing["txn_id"]:
                raise ValueError(
                    "That invoice already exists in QuickBooks, so it cannot be "
                    "marked as not needed. Delete it in QuickBooks first."
                )
            connection.execute(
                """
                INSERT INTO invoice_state (source_id, mode, kind, status, updated_at)
                VALUES (?, ?, ?, 'not_needed', ?)
                ON CONFLICT(source_id, mode, kind) DO UPDATE SET
                    status='not_needed', error_message=NULL, updated_at=excluded.updated_at
                """,
                (source_id, mode, kind, _now()),
            )

    def invoice_states(self, source_ids: list[str], mode: str) -> dict[str, dict[str, InvoiceState]]:
        """Every invoice held for these rows, as {source_id: {kind: state}}."""
        if not source_ids:
            return {}
        placeholders = ",".join("?" for _ in source_ids)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM invoice_state WHERE mode=? AND source_id IN ({placeholders})",
                (mode, *source_ids),
            ).fetchall()
        result: dict[str, dict[str, InvoiceState]] = {}
        for row in rows:
            result.setdefault(row["source_id"], {})[row["kind"]] = InvoiceState(
                kind=row["kind"],
                status=row["status"],
                txn_id=row["txn_id"],
                ref_number=row["ref_number"],
                error_message=row["error_message"],
            )
        return result

    def states_for(self, source_ids: list[str], mode: str) -> dict[str, RecordState]:
        if not source_ids:
            return {}
        placeholders = ",".join("?" for _ in source_ids)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM record_state WHERE mode=? AND source_id IN ({placeholders})",
                (mode, *source_ids),
            ).fetchall()
        return {row["source_id"]: _state(row) for row in rows}

    def overrides(self) -> dict[str, dict[str, str]]:
        """Every manual correction, as {source_id: {field: value}}."""
        result: dict[str, dict[str, str]] = {}
        with self.connection() as connection:
            for row in connection.execute("SELECT source_id, field, value FROM row_overrides"):
                result.setdefault(row["source_id"], {})[row["field"]] = row["value"]
        return result

    def set_override(self, source_id: str, field: str, value: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO row_overrides (source_id, field, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id, field) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
                """,
                (source_id, field, value, _now()),
            )

    def forget_record(self, source_id: str, mode: str) -> bool:
        """Drop what this machine remembers about creating a row's customer.

        Used when the sheet puts a row back in the queue. The memory is a
        shortcut - it lets a re-send skip work already done - and a shortcut
        held past its usefulness is worse than none: the customer may since
        have been deleted in QuickBooks, and a row that skips on the strength
        of this would never be created again.

        The events are left alone. They are the log of what happened, and that
        happened.
        """
        with self.connection() as connection:
            changed = connection.execute(
                "DELETE FROM record_state WHERE source_id=? AND mode=?", (source_id, mode)
            ).rowcount
        return bool(changed)

    def clear_override(self, source_id: str, field: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM row_overrides WHERE source_id=? AND field=?", (source_id, field)
            )

    def recent_events(self, limit: int = 30) -> list[Event]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Event(
                source_id=row["source_id"],
                label=row["label"],
                mode=row["mode"],
                status=row["status"],
                message=row["message"],
                created_at=row["created_at"],
                operation=row["operation"] or "",
                steps=tuple(json.loads(row["steps_json"] or "[]")),
                error=row["error_message"] or "",
                row_number=row["row_number"] or 0,
            )
            for row in rows
        ]


def _label(record: WorkRecord) -> str:
    who = record.job_full_name or f"Row {record.row_number}"
    return f"{who} - invoice {record.invoice_number}" if record.invoice_number else who


def _state(row: sqlite3.Row) -> RecordState:
    return RecordState(
        source_id=row["source_id"],
        mode=row["mode"],
        status=row["status"],
        customer_list_id=row["customer_list_id"],
        job_list_id=row["job_list_id"],
        invoice_txn_id=row["invoice_txn_id"],
        invoice_ref_number=row["invoice_ref_number"],
        error_message=row["error_message"],
        updated_at=row["updated_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

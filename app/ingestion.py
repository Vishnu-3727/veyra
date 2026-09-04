"""Data ingestion, validation, and normalization.

Reads the three raw source CSVs, validates required fields, coerces types,
and loads a clean (or clearly-flagged-invalid) snapshot into SQLite. Nothing
here makes a matching decision -- it only establishes whether each record is
even usable as evidence.

All three sources share one shape (read -> key check -> parse amount/date ->
insert -> tally), so they share one `_ingest` driven by the `_SOURCES` table
below. Only payments persists a per-row validation verdict; the other two
just count.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional

from app import db
from app.normalization import parse_amount, parse_date


@dataclass
class IngestionReport:
    source: str
    rows_read: int
    rows_valid: int
    rows_invalid: int
    errors: list[str] = field(default_factory=list)


class Source(NamedTuple):
    table: str
    filename: str
    key: str                        # primary key column; a blank value drops the row entirely
    columns: tuple[str, ...]        # DB columns filled verbatim from the CSV (amount/amount_raw overridden below)
    required: tuple[str, ...] = ()  # extra non-parsed fields that must be present
    date_col: Optional[str] = None  # column that must parse as a date, if any
    track_validation: bool = False  # persist validation_status/validation_error columns


_SOURCES = (
    Source(
        "payments", "payments.csv", "payment_id",
        ("payment_id", "order_id", "amount_raw", "amount", "currency", "method", "customer_name",
         "customer_email", "created_at", "status", "description"),
        required=("order_id", "customer_name"), date_col="created_at", track_validation=True,
    ),
    Source(
        "bank_settlements", "bank_settlements.csv", "bank_ref",
        ("bank_ref", "utr", "settlement_date", "amount", "narration", "payer_name", "reference_hint"),
        date_col="settlement_date",
    ),
    Source(
        "invoices", "invoices.csv", "invoice_id",
        ("invoice_id", "order_id", "amount", "customer_name", "invoice_date", "description", "status"),
    ),
)


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Required source file not found: {path}")
    with open(path, newline="") as f:
        return list(csv.DictReader(f, restval=""))


def _ingest(conn, path: Path, spec: Source) -> IngestionReport:
    rows = _read_rows(path)
    rows_valid = rows_invalid = 0
    errors: list[str] = []

    for row in rows:
        key_value = (row.get(spec.key) or "").strip()
        if not key_value:
            # cannot even key this record; must be dropped, not silently guessed
            errors.append(f"row skipped: missing {spec.key}")
            rows_invalid += 1
            continue

        amount = parse_amount(row.get("amount"))
        missing = [f for f in spec.required if not str(row.get(f, "")).strip()]
        if amount is None:
            missing.append("amount")
        if spec.date_col and parse_date(row.get(spec.date_col)) is None:
            missing.append(spec.date_col)

        values = {c: row.get(c, "") for c in spec.columns}
        values[spec.key] = key_value
        values["amount"] = amount
        if "amount_raw" in values:
            values["amount_raw"] = row.get("amount", "")
        if spec.track_validation:
            values["validation_status"] = "invalid" if missing else "ok"
            values["validation_error"] = (
                f"missing/corrupt field(s): {', '.join(sorted(set(missing)))}" if missing else None
            )

        if missing:
            rows_invalid += 1
        else:
            rows_valid += 1

        # table/column names come from the static _SOURCES table above, never from input
        conn.execute(
            f"INSERT OR REPLACE INTO {spec.table} ({', '.join(values)}) "
            f"VALUES ({','.join('?' * len(values))})",
            tuple(values.values()),
        )

    return IngestionReport(spec.table, len(rows), rows_valid, rows_invalid, errors)


def ingest_all(raw_dir: Path) -> dict[str, IngestionReport]:
    """Ingest all three sources in one transaction. Replaces the current batch."""
    db.init_db()
    with db.get_conn() as conn:
        db.reset_source_tables(conn)
        return {s.table: _ingest(conn, raw_dir / s.filename, s) for s in _SOURCES}

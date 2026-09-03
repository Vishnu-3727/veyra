"""Data ingestion, validation, and normalization.

Reads the three raw source CSVs, validates required fields, coerces types,
and loads a clean (or clearly-flagged-invalid) snapshot into SQLite. Nothing
here makes a matching decision -- it only establishes whether each record is
even usable as evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

from app import db
from app.normalization import parse_amount, parse_date


@dataclass
class IngestionReport:
    source: str
    rows_read: int
    rows_valid: int
    rows_invalid: int
    errors: list[str] = field(default_factory=list)


REQUIRED_PAYMENT_FIELDS = ["payment_id", "order_id", "amount", "created_at", "customer_name"]
REQUIRED_BANK_FIELDS = ["bank_ref", "settlement_date", "amount"]
REQUIRED_INVOICE_FIELDS = ["invoice_id", "order_id", "amount"]


def _read_csv_safely(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Required source file not found: {path}")
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    return df


def ingest_payments(conn, path: Path) -> IngestionReport:
    df = _read_csv_safely(path)
    rows_valid = rows_invalid = 0
    errors: list[str] = []
    for _, row in df.iterrows():
        pid = (row.get("payment_id") or "").strip()
        missing = [f for f in REQUIRED_PAYMENT_FIELDS if not str(row.get(f, "")).strip()]
        amount = parse_amount(row.get("amount"))
        created_at = parse_date(row.get("created_at"))
        validation_status = "ok"
        validation_error = None
        if not pid:
            errors.append("row skipped: missing payment_id")
            rows_invalid += 1
            continue  # cannot even key this record; must be dropped, not silently guessed
        if missing or amount is None or created_at is None:
            validation_status = "invalid"
            reasons = list(missing)
            if amount is None:
                reasons.append("amount")
            if created_at is None:
                reasons.append("created_at")
            validation_error = f"missing/corrupt field(s): {', '.join(sorted(set(reasons)))}"
            rows_invalid += 1
        else:
            rows_valid += 1
        conn.execute(
            """INSERT OR REPLACE INTO payments
               (payment_id, order_id, amount_raw, amount, currency, method, customer_name,
                customer_email, created_at, status, description, validation_status, validation_error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid, row.get("order_id", ""), row.get("amount", ""), amount,
                row.get("currency", ""), row.get("method", ""), row.get("customer_name", ""),
                row.get("customer_email", ""), row.get("created_at", ""), row.get("status", ""),
                row.get("description", ""), validation_status, validation_error,
            ),
        )
    return IngestionReport("payments", len(df), rows_valid, rows_invalid, errors)


def ingest_bank_settlements(conn, path: Path) -> IngestionReport:
    df = _read_csv_safely(path)
    rows_valid = rows_invalid = 0
    errors: list[str] = []
    for _, row in df.iterrows():
        ref = (row.get("bank_ref") or "").strip()
        amount = parse_amount(row.get("amount"))
        settle_date = parse_date(row.get("settlement_date"))
        if not ref:
            errors.append("row skipped: missing bank_ref")
            rows_invalid += 1
            continue
        if amount is None or settle_date is None:
            rows_invalid += 1
        else:
            rows_valid += 1
        conn.execute(
            """INSERT OR REPLACE INTO bank_settlements
               (bank_ref, utr, settlement_date, amount, narration, payer_name, reference_hint)
               VALUES (?,?,?,?,?,?,?)""",
            (
                ref, row.get("utr", ""), row.get("settlement_date", ""), amount,
                row.get("narration", ""), row.get("payer_name", ""), row.get("reference_hint", ""),
            ),
        )
    return IngestionReport("bank_settlements", len(df), rows_valid, rows_invalid, errors)


def ingest_invoices(conn, path: Path) -> IngestionReport:
    df = _read_csv_safely(path)
    rows_valid = rows_invalid = 0
    errors: list[str] = []
    for _, row in df.iterrows():
        inv_id = (row.get("invoice_id") or "").strip()
        amount = parse_amount(row.get("amount"))
        if not inv_id:
            errors.append("row skipped: missing invoice_id")
            rows_invalid += 1
            continue
        if amount is None:
            rows_invalid += 1
        else:
            rows_valid += 1
        conn.execute(
            """INSERT OR REPLACE INTO invoices
               (invoice_id, order_id, amount, customer_name, invoice_date, description, status)
               VALUES (?,?,?,?,?,?,?)""",
            (
                inv_id, row.get("order_id", ""), amount, row.get("customer_name", ""),
                row.get("invoice_date", ""), row.get("description", ""), row.get("status", ""),
            ),
        )
    return IngestionReport("invoices", len(df), rows_valid, rows_invalid, errors)


def ingest_all(raw_dir: Path) -> dict[str, IngestionReport]:
    """Ingest all three sources in one transaction. Replaces the current batch."""
    db.init_db()
    with db.get_conn() as conn:
        db.reset_source_tables(conn)
        reports = {
            "payments": ingest_payments(conn, raw_dir / "payments.csv"),
            "bank_settlements": ingest_bank_settlements(conn, raw_dir / "bank_settlements.csv"),
            "invoices": ingest_invoices(conn, raw_dir / "invoices.csv"),
        }
    return reports

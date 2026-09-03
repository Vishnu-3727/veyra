"""SQLite persistence layer.

Source tables (payments/bank_settlements/invoices) hold the current batch
and are replaced on each ingestion. decisions/audit_log/exceptions/runs are
append-only across runs (tagged by run_id) so historical audit trails survive
re-runs even if the underlying source batch changes.

Plain sqlite3 (stdlib) is used deliberately -- no ORM. The schema is small
and the queries are simple enough that an ORM would add indirection without
value for a 3-day hackathon build.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    order_id TEXT,
    amount_raw TEXT,
    amount REAL,
    currency TEXT,
    method TEXT,
    customer_name TEXT,
    customer_email TEXT,
    created_at TEXT,
    status TEXT,
    description TEXT,
    validation_status TEXT NOT NULL DEFAULT 'ok',
    validation_error TEXT
);

CREATE TABLE IF NOT EXISTS bank_settlements (
    bank_ref TEXT PRIMARY KEY,
    utr TEXT,
    settlement_date TEXT,
    amount REAL,
    narration TEXT,
    payer_name TEXT,
    reference_hint TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id TEXT PRIMARY KEY,
    order_id TEXT,
    amount REAL,
    customer_name TEXT,
    invoice_date TEXT,
    description TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    total_payments INTEGER,
    ai_enabled INTEGER,
    metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    status TEXT NOT NULL,             -- AUTO_MATCH | AI_ASSISTED_MATCH | EXCEPTION
    category TEXT,                    -- exception category, null for matches
    matched_bank_ref TEXT,
    confidence INTEGER,
    method TEXT NOT NULL,             -- rule | ai | validation
    ai_used INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    invoice_id TEXT,
    invoice_status TEXT,              -- found_consistent | found_mismatch | not_found
    evidence_json TEXT,
    processing_ms REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_decisions_payment ON decisions(payment_id);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);

CREATE TABLE IF NOT EXISTS exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    decision_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    suggested_action TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exceptions_run ON exceptions(run_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    decision_id INTEGER NOT NULL,
    actor TEXT NOT NULL,              -- rule_engine | ai_assisted | policy_guardrail | validator
    status TEXT NOT NULL,
    category TEXT,
    ai_used INTEGER NOT NULL DEFAULT 0,
    confidence INTEGER,
    reason TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def reset_source_tables(conn: sqlite3.Connection) -> None:
    """Clear the current-batch source tables ahead of a fresh ingestion."""
    conn.execute("DELETE FROM payments")
    conn.execute("DELETE FROM bank_settlements")
    conn.execute("DELETE FROM invoices")


def dumps(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)

"""SQLite persistence layer.

Source tables (payments/bank_settlements/invoices) hold the current batch
and are replaced on each ingestion. decisions/audit_log/exceptions/runs are
append-only across runs (tagged by run_id) so historical audit trails survive
re-runs even if the underlying source batch changes. run_ground_truth is a
per-run snapshot of data/raw/ground_truth.csv taken at reconciliation time, so
evaluating an old run_id later (after the raw dataset has since been
regenerated/overwritten) still scores that run against the ground truth it
actually ran against, not whatever happens to be on disk now.

Plain sqlite3 (stdlib) is used deliberately -- no ORM. The schema is small
and the queries are simple enough that an ORM would add indirection without
value for a 3-day hackathon build.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import config
from app import constants as C

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
    reference_hint TEXT,
    validation_status TEXT NOT NULL DEFAULT 'ok',
    validation_error TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id TEXT PRIMARY KEY,
    order_id TEXT,
    amount REAL,
    customer_name TEXT,
    invoice_date TEXT,
    description TEXT,
    status TEXT,
    validation_status TEXT NOT NULL DEFAULT 'ok',
    validation_error TEXT
);

-- A run row is created at the START of reconciliation with status='RUNNING' and updated to
-- COMPLETED/FAILED at the end. Creating it last (the old behavior) meant a crash mid-batch left
-- decisions/audit_log/exceptions/run_* rows referencing a run_id with no run record at all.
-- A killed process now leaves an honest 'RUNNING' row instead of orphaned execution records.
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    total_payments INTEGER,
    ai_enabled INTEGER,
    metrics_json TEXT,
    status TEXT NOT NULL DEFAULT 'COMPLETED',   -- RUNNING | COMPLETED | FAILED
    error TEXT,                                 -- failure detail when status='FAILED'
    ai_provider TEXT,                           -- AI config frozen for this run (never the key)
    ai_model TEXT,
    ai_timeout_seconds REAL,
    dataset_fingerprint TEXT,                   -- sha256 over the ordered source files
    dataset_seed INTEGER,
    dataset_size INTEGER,
    ground_truth_snapshotted INTEGER NOT NULL DEFAULT 0
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
    actor TEXT NOT NULL,              -- rule_engine | ai_assisted | policy_guardrail | validator | human_reviewer
    status TEXT NOT NULL,
    category TEXT,
    ai_used INTEGER NOT NULL DEFAULT 0,
    confidence INTEGER,
    reason TEXT,
    evidence_json TEXT,
    exception_id INTEGER,             -- set only on human-review events, so provenance is traceable
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS run_ground_truth (
    run_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    true_bank_ref TEXT,
    true_invoice_id TEXT,
    case_type TEXT,
    is_safely_resolvable INTEGER NOT NULL,
    notes TEXT,
    PRIMARY KEY (run_id, payment_id)
);

-- Per-run snapshot of the payment rows this run actually processed. The `payments` table holds
-- only the CURRENT batch (it is replaced on every ingestion), so historical endpoints must read
-- payment metadata from here instead -- otherwise ingesting a new dataset would silently orphan
-- every earlier run's decisions when they are joined back to payment metadata.
CREATE TABLE IF NOT EXISTS run_payments (
    run_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    order_id TEXT,
    amount REAL,
    currency TEXT,
    method TEXT,
    customer_name TEXT,
    customer_email TEXT,
    created_at TEXT,
    status TEXT,
    description TEXT,
    validation_status TEXT,
    validation_error TEXT,
    PRIMARY KEY (run_id, payment_id)
);

-- Same reasoning as run_payments, for the other two sources. These make a completed run
-- self-contained: its decisions, its evaluation ground truth AND its naive-baseline comparison
-- can all be recomputed from the data the run actually saw, instead of from whatever dataset
-- happens to be on disk later.
CREATE TABLE IF NOT EXISTS run_bank_settlements (
    run_id TEXT NOT NULL,
    bank_ref TEXT NOT NULL,
    utr TEXT,
    settlement_date TEXT,
    amount REAL,
    narration TEXT,
    payer_name TEXT,
    reference_hint TEXT,
    validation_status TEXT,
    validation_error TEXT,
    PRIMARY KEY (run_id, bank_ref)
);

CREATE TABLE IF NOT EXISTS run_invoices (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    order_id TEXT,
    amount REAL,
    customer_name TEXT,
    invoice_date TEXT,
    description TEXT,
    status TEXT,
    validation_status TEXT,
    validation_error TEXT,
    PRIMARY KEY (run_id, invoice_id)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: readers (the dashboard polling /decisions, /exceptions, ...) are not blocked by the
    # writer committing each decision during a run. synchronous=NORMAL: the pipeline commits once
    # per decision, so a full fsync per record would dominate the batch; NORMAL still survives a
    # process crash (only an OS/power loss can lose the most recent commits), which is the right
    # trade for a local, re-runnable analytical batch whose source data is on disk anyway.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# Columns added after the first schema version. `CREATE TABLE IF NOT EXISTS` is a no-op on an
# existing table, so a database created by an earlier build needs these added explicitly --
# otherwise a returning demo/judge with an existing data/finance.db would hit "no such column".
_ADDED_COLUMNS = (
    ("bank_settlements", "validation_status", "TEXT NOT NULL DEFAULT 'ok'"),
    ("bank_settlements", "validation_error", "TEXT"),
    ("invoices", "validation_status", "TEXT NOT NULL DEFAULT 'ok'"),
    ("invoices", "validation_error", "TEXT"),
    ("audit_log", "exception_id", "INTEGER"),
    # Legacy runs default to COMPLETED: they are historical rows that did finish (the old code
    # only ever inserted a run row after a successful batch), so that is the honest backfill.
    ("runs", "status", "TEXT NOT NULL DEFAULT 'COMPLETED'"),
    ("runs", "error", "TEXT"),
    ("runs", "ai_provider", "TEXT"),
    ("runs", "ai_model", "TEXT"),
    ("runs", "ai_timeout_seconds", "REAL"),
    ("runs", "dataset_fingerprint", "TEXT"),
    ("runs", "dataset_seed", "INTEGER"),
    ("runs", "dataset_size", "INTEGER"),
    ("runs", "ground_truth_snapshotted", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    _restrict_db_permissions()


def _restrict_db_permissions() -> None:
    """Keep the database readable only by its owner.

    `app_settings` stores the operator's LLM API key, so the database file is a credential
    store, not just data. SQLite creates it with the process umask (commonly 0644 -- readable
    by every local account), and the WAL/SHM siblings inherit the same. POSIX only; a failure
    here must never stop the app, so it is best-effort.
    """
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{config.DB_PATH}{suffix}")
        try:
            if path.exists():
                path.chmod(0o600)
        except OSError:
            pass


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def latest_run_id() -> Optional[str]:
    """Most recently started run of any status, or None if nothing has been run yet."""
    with get_conn() as conn:
        row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None


def latest_completed_run_id() -> Optional[str]:
    """Most recently started run that actually finished successfully.

    Anything comparing or summarizing a whole batch (the naive baseline, for instance) must not
    silently pick up a run that is still in flight or that failed part-way through.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT run_id FROM runs WHERE status = ? ORDER BY started_at DESC LIMIT 1",
            (C.RUN_COMPLETED,),
        ).fetchone()
    return row["run_id"] if row else None


def run_exists(run_id: str) -> bool:
    """Whether a run row exists, regardless of status.

    Endpoints that accept an explicit `run_id` use this to answer 404 instead of quietly
    substituting some other run's (or the current dataset's) numbers.
    """
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is not None


def reset_source_tables(conn: sqlite3.Connection) -> None:
    """Clear the current-batch source tables ahead of a fresh ingestion."""
    conn.execute("DELETE FROM payments")
    conn.execute("DELETE FROM bank_settlements")
    conn.execute("DELETE FROM invoices")


def dumps(obj) -> str:
    """Serialize evidence/metrics for storage.

    `allow_nan=False` is deliberate: `json.dumps` would otherwise emit bare `NaN`/`Infinity`,
    which is not valid JSON, so a non-finite money value could land in the database and then
    break every consumer's `JSON.parse`. Ingestion already rejects non-finite amounts and every
    ratio here is zero-guarded, so this is unreachable in practice -- but a financial record must
    fail loudly rather than persist a number no reader can parse.
    """
    return json.dumps(obj, default=str, ensure_ascii=False, allow_nan=False)

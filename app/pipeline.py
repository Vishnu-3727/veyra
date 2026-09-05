"""Reconciliation pipeline orchestrator.

Ties ingestion -> candidate generation -> deterministic scoring -> AI
escalation -> policy verification -> invoice corroboration -> persistence
together into a single, timed, auditable batch run.

This is the only module that writes decisions/audit_log/exceptions rows --
every other module is a pure function operating on in-memory data.

Transaction shape matters here. Source data is read up front into memory, and
each decision is then committed in its OWN short transaction, so a network
round-trip to an LLM provider never happens while a write transaction is open
(a batch-long transaction would hold the SQLite write lock for the whole run
and block the API's own reads). One decision still lands atomically: its
decisions row, its audit event, and its exception row commit together or not
at all.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from app import constants as C
from app import db
from app import settings as llm_settings
from app.ai_reasoning import AIResult, reason_about_candidates
from app.candidates import BankCandidateIndex, generate_candidates
from app.evaluation import load_ground_truth
from app.exceptions import SUGGESTED_ACTIONS, build_exception_detail
from app.ingestion import ingest_all
from app.normalization import normalized_bank_ref_text, ref_fragment, ref_match_normalized
from app.policy import apply_ai_policy
from app.scoring import NEEDS_AI, decide_deterministic
import config
from config import THRESHOLDS

# AI outcomes that mean "this provider is not currently usable": a transport/timeout failure
# (ERROR) or a provider that answers with content violating the required output schema
# (INVALID). Either way, repeating the call for every remaining record only wastes the batch's
# time, so both feed the circuit breaker.
_AI_FAILURE_DECISIONS = ("ERROR", "INVALID")

_PAYMENT_SNAPSHOT_COLUMNS = (
    "payment_id", "order_id", "amount", "currency", "method", "customer_name", "customer_email",
    "created_at", "status", "description", "validation_status", "validation_error",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Batch:
    """Everything a run needs from the source tables, read once, up front."""

    def __init__(self, conn):
        self.payments = [dict(r) for r in conn.execute("SELECT * FROM payments").fetchall()]
        # Raw rows are kept as-read (invalid ones included) because the per-run source snapshot
        # must record exactly what the run saw, not only the subset it could use as evidence.
        self.bank_rows = [dict(r) for r in conn.execute("SELECT * FROM bank_settlements").fetchall()]
        self.invoice_rows = [dict(r) for r in conn.execute("SELECT * FROM invoices").fetchall()]

        # Malformed source rows are preserved by ingestion but must never participate in
        # matching -- an unusable record is not evidence. Their existence is still tracked so a
        # reviewer can tell "no bank/invoice record" apart from "record present but unreadable".
        valid_bank = [b for b in self.bank_rows if b.get("validation_status") != "invalid"]
        self.index = BankCandidateIndex(valid_bank)
        self.invalid_bank = [
            (normalized_bank_ref_text(b.get("utr") or "", b.get("narration") or "", b.get("reference_hint") or ""),
             b["bank_ref"])
            for b in self.bank_rows if b.get("validation_status") == "invalid"
        ]

        self.invoices_by_order: dict[str, list[dict]] = {}
        self.invalid_invoice_orders: dict[str, list[str]] = {}
        for inv in self.invoice_rows:
            order_id = inv.get("order_id")
            if not order_id:
                continue
            if inv.get("validation_status") == "invalid":
                self.invalid_invoice_orders.setdefault(order_id, []).append(inv["invoice_id"])
            else:
                self.invoices_by_order.setdefault(order_id, []).append(inv)

    def invalid_bank_refs_matching(self, order_frag: str) -> list[str]:
        """Invalid bank rows whose reference text still traces back to this payment."""
        if not order_frag:
            return []
        return [ref for text, ref in self.invalid_bank if ref_match_normalized(order_frag, text) != "NONE"]


def _corroborate_invoice(payment: dict, batch_invoices: dict[str, list[dict]], thresholds,
                         invalid_orders: dict[str, list[str]] | None = None) -> tuple[dict | None, str]:
    order_id = payment.get("order_id")
    invs = batch_invoices.get(order_id) or []
    if not invs:
        if invalid_orders and invalid_orders.get(order_id):
            return None, C.INVOICE_RECORD_INVALID
        return None, C.INVOICE_NOT_FOUND
    if len(invs) > 1:
        # Multiple invoices share this order_id -- secondary evidence is itself ambiguous. Surface
        # that explicitly rather than silently picking (and thereby discarding) one of them.
        return invs[0], C.INVOICE_AMBIGUOUS
    inv = invs[0]
    p_amount = payment.get("amount")
    i_amount = inv.get("amount")
    if p_amount is None or i_amount is None:
        return inv, C.INVOICE_FOUND_MISMATCH
    diff_pct = abs(i_amount - p_amount) / p_amount if p_amount else 1.0
    if diff_pct <= thresholds.invoice_amount_tolerance_pct:
        return inv, C.INVOICE_FOUND_CONSISTENT
    return inv, C.INVOICE_FOUND_MISMATCH


def _snapshot_ground_truth(conn, run_id: str, raw_dir: Path) -> bool:
    """Persist the ground truth this run is actually being scored against, so a later
    `evaluate(run_id)` reads the truth that was current AT RUN TIME even if the raw dataset on
    disk has since been regenerated/overwritten by another run. Best-effort: some datasets
    (e.g. hand-supplied ones) may not ship a ground_truth.csv at all, which is fine -- evaluation
    falls back to the raw file in that case."""
    try:
        gt = load_ground_truth(raw_dir)
    except FileNotFoundError:
        return False
    conn.executemany(
        """INSERT OR REPLACE INTO run_ground_truth
           (run_id, payment_id, true_bank_ref, true_invoice_id, case_type, is_safely_resolvable, notes)
           VALUES (?,?,?,?,?,?,?)""",
        [
            (run_id, payment_id, g.get("true_bank_ref", ""), g.get("true_invoice_id", ""), g.get("case_type", ""),
             int(bool(g.get("is_safely_resolvable"))), g.get("notes", ""))
            for payment_id, g in gt.items()
        ],
    )
    return True


_BANK_SNAPSHOT_COLUMNS = (
    "bank_ref", "utr", "settlement_date", "amount", "narration", "payer_name", "reference_hint",
    "validation_status", "validation_error",
)
_INVOICE_SNAPSHOT_COLUMNS = (
    "invoice_id", "order_id", "amount", "customer_name", "invoice_date", "description", "status",
    "validation_status", "validation_error",
)
_SOURCE_FILES = ("payments.csv", "bank_settlements.csv", "invoices.csv", "ground_truth.csv")


def _snapshot_source(conn, table: str, columns: tuple[str, ...], run_id: str, rows: list[dict]) -> None:
    """Freeze one source table for this run.

    The live source tables only ever hold the CURRENT batch (they are replaced on every
    ingestion), so without these snapshots a completed run stops being self-contained the moment
    a new dataset is ingested: historical decisions lose their payment metadata and a historical
    naive-baseline comparison would silently be computed against a different dataset.
    """
    column_sql = ", ".join(("run_id",) + columns)
    placeholders = ",".join("?" * (len(columns) + 1))
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} ({column_sql}) VALUES ({placeholders})",
        [tuple([run_id] + [r.get(c) for c in columns]) for r in rows],
    )


def _dataset_provenance(raw_dir: Path) -> dict:
    """Fingerprint the source files this run read, plus the generator's seed/size if available.

    Provenance metadata only -- the authoritative record of what the run processed is the
    per-run source snapshots. This exists so the audit trail can answer "which exact dataset
    produced this run?" without diffing snapshots.
    """
    per_file: dict[str, str] = {}
    combined = hashlib.sha256()
    for name in _SOURCE_FILES:
        path = raw_dir / name
        if not path.exists():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        per_file[name] = digest
        combined.update(name.encode())
        combined.update(digest.encode())

    seed = size = None
    summary_path = raw_dir / "generation_summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text())
            seed, size = summary.get("seed"), summary.get("payments")
        except (json.JSONDecodeError, OSError):
            pass  # provenance is best-effort; a hand-supplied dataset has no generator summary
    return {
        "fingerprint": combined.hexdigest() if per_file else None,
        "source_fingerprints": per_file,
        "seed": seed,
        "size": size,
    }


def _open_run(conn, run_id: str, started_at: str, ai: "llm_settings.LLMSettings") -> None:
    """Record the run as RUNNING before any decision row exists.

    Writing the run row only after a successful batch (the old behavior) meant a crash, kill, or
    database error mid-run left decisions/audit_log/exceptions/run_* rows pointing at a run_id
    with no run record -- orphaned execution history, which an audit system must not produce.
    The stored AI configuration deliberately excludes the API key.
    """
    conn.execute(
        """INSERT INTO runs (run_id, started_at, finished_at, total_payments, ai_enabled,
                             metrics_json, status, ai_provider, ai_model, ai_timeout_seconds)
           VALUES (?,?,NULL,0,?,?,?,?,?,?)""",
        (run_id, started_at, int(ai.enabled), db.dumps({"run_id": run_id, "status": C.RUN_RUNNING}),
         C.RUN_RUNNING, ai.provider, ai.model, ai.timeout_seconds),
    )


def run_reconciliation(raw_dir: Path | None = None) -> dict:
    """Ingest the current raw dataset and run the full reconciliation batch.

    Returns a summary dict with counts, timing, and throughput. Full per-record results are
    persisted to SQLite (decisions/audit_log/exceptions), and the run's own lifecycle is durable:
    the `runs` row exists as RUNNING from the start and ends as COMPLETED or FAILED.

    The AI configuration is frozen at run start: an operator changing provider/model/key from the
    dashboard mid-batch does not split one run across two providers, which would make the run's
    metrics and audit trail impossible to interpret. The next run picks up the new settings.
    """
    from config import RAW_DIR

    raw_dir = raw_dir or RAW_DIR
    run_id = uuid.uuid4().hex[:12]
    started_at = _now_iso()
    t_start = time.perf_counter()
    run_ai = llm_settings.get()  # frozen for the whole batch, key included in memory only

    # The run row is written before ingestion, so the schema has to exist before that -- a
    # first-ever CLI run (`python cli.py run` on a fresh checkout, no API ever started) would
    # otherwise fail on `no such table: runs`. Idempotent, and applies pending migrations.
    db.init_db()

    with db.get_conn() as conn:
        _open_run(conn, run_id, started_at, run_ai)

    try:
        return _execute_run(run_id, started_at, t_start, raw_dir, run_ai)
    except BaseException as exc:  # noqa: BLE001 - the run's own failure must be recorded, then re-raised
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE run_id = ?",
                (C.RUN_FAILED, _now_iso(), f"{type(exc).__name__}: {exc}", run_id),
            )
        raise


def _prefetch_ai(pending, run_ai):
    """Reason about every AI-eligible case up front, `config.AI_CONCURRENCY` at a time.

    `pending` is [(payment_index, payment, ai_candidates)] in payment order. Returns
    ({payment_index: AIResult}, circuit_open, circuit_reason).

    Why this exists: AI escalation is the only network-bound step in a run, and issuing those
    calls one at a time made a batch take (cases x per-call latency) -- minutes of wall clock
    spent almost entirely waiting on sockets. The calls are independent (each looks at one
    payment and its own candidates) so they parallelize without changing any decision.

    The circuit breaker keeps its meaning -- "stop paying the per-call timeout once the provider
    is clearly broken" -- but its unit is now a chunk rather than a single call. Results are
    still examined in payment order, so which cases trip it is deterministic; the cost of
    concurrency is that up to one chunk of calls may already be in flight when it trips. That
    bound is what keeps the worst case honest: at most AI_CONCURRENCY wasted calls, not the
    whole remaining batch.
    """
    results: dict[int, AIResult] = {}
    consecutive = 0
    circuit_open = False
    circuit_reason = ""
    width = max(1, config.AI_CONCURRENCY)

    with ThreadPoolExecutor(max_workers=width, thread_name_prefix="veyra-ai-batch") as pool:
        for start in range(0, len(pending), width):
            chunk = pending[start:start + width]
            if circuit_open:
                break
            # reason_about_candidates never raises -- every failure path returns an
            # AIResult(decision="ERROR"), so a chunk cannot lose results to an exception.
            for (idx, _payment, _cands), result in zip(chunk, pool.map(
                    lambda item: reason_about_candidates(item[1], item[2], settings=run_ai), chunk)):
                results[idx] = result
            # Ordered replay: identical accounting to the original serial loop.
            for idx, _payment, _cands in chunk:
                if results[idx].decision in _AI_FAILURE_DECISIONS:
                    consecutive += 1
                    if consecutive >= THRESHOLDS.ai_circuit_breaker_threshold:
                        circuit_open = True
                        circuit_reason = results[idx].error or "repeated failures"
                else:
                    consecutive = 0

    return results, circuit_open, circuit_reason


def _execute_run(run_id: str, started_at: str, t_start: float, raw_dir: Path,
                 run_ai: "llm_settings.LLMSettings") -> dict:
    ingestion_reports = ingest_all(raw_dir)

    # --- read phase: pull the batch into memory, then release the connection ---
    with db.get_conn() as conn:
        batch = _Batch(conn)

    provenance = _dataset_provenance(raw_dir)

    # --- snapshot phase: one short write transaction, before any AI/network work ---
    with db.get_conn() as conn:
        _snapshot_source(conn, "run_payments", _PAYMENT_SNAPSHOT_COLUMNS, run_id, batch.payments)
        _snapshot_source(conn, "run_bank_settlements", _BANK_SNAPSHOT_COLUMNS, run_id, batch.bank_rows)
        _snapshot_source(conn, "run_invoices", _INVOICE_SNAPSHOT_COLUMNS, run_id, batch.invoice_rows)
        ground_truth_snapshotted = _snapshot_ground_truth(conn, run_id, raw_dir)
        conn.execute(
            """UPDATE runs SET dataset_fingerprint = ?, dataset_seed = ?, dataset_size = ?,
                               ground_truth_snapshotted = ? WHERE run_id = ?""",
            (provenance["fingerprint"], provenance["seed"], provenance["size"],
             int(ground_truth_snapshotted), run_id),
        )

    status_counts = {C.STATUS_AUTO_MATCH: 0, C.STATUS_AI_ASSISTED_MATCH: 0, C.STATUS_EXCEPTION: 0}
    category_counts: dict[str, int] = {}
    ai_invocations = 0
    ai_circuit_open = False
    ai_circuit_reason = ""
    total_proc_ms = 0.0

    # Deterministic scoring first, for the whole batch. It is pure CPU work over data already in
    # memory, so doing it up front costs nothing and is what makes the AI-eligible set knowable
    # before any network call is made -- which is what lets those calls run concurrently below.
    scored: dict[int, tuple] = {}
    pending_ai: list[tuple] = []
    for idx, payment in enumerate(batch.payments):
        if payment.get("validation_status") == "invalid":
            continue  # never reaches candidate generation; handled in the loop below
        candidates = generate_candidates(payment, batch.index, THRESHOLDS)
        outcome = decide_deterministic(candidates, THRESHOLDS)
        scored[idx] = (candidates, outcome)
        if outcome.status == NEEDS_AI and run_ai.enabled:
            pending_ai.append((idx, payment, outcome.ai_candidates))

    ai_results: dict[int, AIResult] = {}
    if pending_ai:
        ai_results, ai_circuit_open, ai_circuit_reason = _prefetch_ai(pending_ai, run_ai)

    # One connection, but committed per decision: the write lock is never held across an LLM call.
    conn = db.connect()
    try:
        for idx, payment in enumerate(batch.payments):
            t0 = time.perf_counter()

            if payment.get("validation_status") == "invalid":
                status, category, matched_ref = C.STATUS_EXCEPTION, C.CAT_MISSING_FIELDS, None
                method, ai_used, confidence = "validation", False, None
                reason = f"Record failed validation: {payment.get('validation_error')}"
                evidence = build_exception_detail(category, reason, [], [], [], None)
                _persist(conn, run_id, payment, status, category, matched_ref, confidence, method, ai_used,
                         reason, evidence, None, None)
                conn.commit()
                status_counts[status] += 1
                category_counts[category] = category_counts.get(category, 0) + 1
                total_proc_ms += (time.perf_counter() - t0) * 1000
                continue

            candidates, outcome = scored[idx]

            ai_used = False
            ai_evidence = None
            if outcome.status == C.STATUS_AUTO_MATCH:
                status, category = C.STATUS_AUTO_MATCH, None
                matched_ref, confidence, method = outcome.matched.bank_ref, 100, "rule"
                reason = outcome.reason

            elif outcome.status == NEEDS_AI:
                ai_used = True
                ai_invocations += 1
                if not run_ai.enabled:
                    ai_result = AIResult(decision="ERROR", error="AI disabled: no API key configured",
                                         model=run_ai.model)
                elif ai_circuit_open:
                    ai_result = AIResult(
                        decision="ERROR",
                        error=f"AI circuit breaker open after {THRESHOLDS.ai_circuit_breaker_threshold} "
                              f"consecutive failures this batch ({ai_circuit_reason}); skipping remaining "
                              f"AI calls rather than hanging the batch on a broken provider.",
                    )
                else:
                    # Already obtained by _prefetch_ai (which also owns the circuit breaker).
                    # A case reached after the breaker tripped has no entry -- the same
                    # AI_UNAVAILABLE outcome the serial version produced, without the call.
                    ai_result = ai_results.get(idx) or AIResult(
                        decision="ERROR",
                        error=f"AI circuit breaker open after {THRESHOLDS.ai_circuit_breaker_threshold} "
                              f"consecutive failures this batch ({ai_circuit_reason}); skipping remaining "
                              f"AI calls rather than hanging the batch on a broken provider.",
                        model=run_ai.model)
                ai_evidence = ai_result.to_evidence()
                policy_outcome = apply_ai_policy(ai_result, outcome.ai_candidates, THRESHOLDS)
                method = "ai"
                if policy_outcome.status == C.STATUS_AI_ASSISTED_MATCH:
                    status, category = C.STATUS_AI_ASSISTED_MATCH, None
                    matched_ref, confidence = policy_outcome.matched.bank_ref, ai_result.confidence
                    reason = policy_outcome.reason
                else:
                    status, category, matched_ref, confidence = C.STATUS_EXCEPTION, policy_outcome.category, None, ai_result.confidence
                    reason = policy_outcome.reason

            else:  # deterministic EXCEPTION
                status, category, matched_ref, confidence, method = C.STATUS_EXCEPTION, outcome.category, None, None, "rule"
                reason = outcome.reason

            invoice, invoice_status = _corroborate_invoice(
                payment, batch.invoices_by_order, THRESHOLDS, batch.invalid_invoice_orders,
            )
            invoice_id = invoice["invoice_id"] if invoice else None

            if status == C.STATUS_EXCEPTION:
                evidence = build_exception_detail(
                    category, reason, outcome.considered, outcome.rejected, outcome.duplicate_refs, ai_evidence,
                )
                if category == C.CAT_NO_CANDIDATE:
                    # "No candidate" and "the only matching bank row was unreadable" are different
                    # findings; a reviewer chasing a missing settlement needs to know which it is.
                    unusable = batch.invalid_bank_refs_matching(ref_fragment(payment.get("order_id") or ""))
                    if unusable:
                        evidence["unusable_bank_records_matching_reference"] = unusable
                        reason = (f"{reason} Note: bank record(s) {', '.join(unusable)} reference this payment "
                                  f"but failed source validation, so they could not be used as evidence.")
                        evidence["why_unresolved"] = reason
            else:
                evidence = {
                    "matched_candidate": next(
                        (c.to_evidence() for c in (outcome.considered or []) if c.bank_ref == matched_ref), None
                    ),
                    "other_candidates_considered": [c.to_evidence() for c in outcome.considered if c.bank_ref != matched_ref],
                    "duplicate_bank_refs": outcome.duplicate_refs,
                    "ai_evidence": ai_evidence,
                    "invoice": {"invoice_id": invoice_id, "status": invoice_status},
                }

            proc_ms = (time.perf_counter() - t0) * 1000
            total_proc_ms += proc_ms
            _persist(conn, run_id, payment, status, category, matched_ref, confidence, method, ai_used,
                     reason, evidence, invoice_id, invoice_status, proc_ms)
            conn.commit()  # one decision + its audit event + its exception land atomically

            status_counts[status] += 1
            if category:
                category_counts[category] = category_counts.get(category, 0) + 1
    finally:
        conn.close()

    elapsed = time.perf_counter() - t_start
    n = len(batch.payments)
    metrics = {
        "run_id": run_id,
        "status": C.RUN_COMPLETED,
        "total_payments": n,
        "status_counts": status_counts,
        "category_counts": category_counts,
        "ai_invocations": ai_invocations,
        "ai_circuit_breaker_tripped": ai_circuit_open,
        "ai_circuit_breaker_reason": ai_circuit_reason or None,
        "ai_enabled": run_ai.enabled,
        # AI configuration frozen for this run. Never the API key -- only what is needed to
        # interpret the run's AI outcomes later.
        "ai_config": {
            "provider": run_ai.provider, "model": run_ai.model,
            "timeout_seconds": run_ai.timeout_seconds, "enabled": run_ai.enabled,
        },
        "total_processing_seconds": round(elapsed, 3),
        "throughput_per_second": round(n / elapsed, 2) if elapsed > 0 else None,
        "avg_processing_ms_per_record": round(total_proc_ms / n, 3) if n else None,
        "ground_truth_snapshotted": ground_truth_snapshotted,
        "dataset_fingerprint": provenance["fingerprint"],
        "dataset_source_fingerprints": provenance["source_fingerprints"],
        "dataset_seed": provenance["seed"],
        "dataset_size": provenance["size"],
        "ingestion_reports": {
            k: {"rows_read": v.rows_read, "rows_valid": v.rows_valid, "rows_invalid": v.rows_invalid, "errors": v.errors}
            for k, v in ingestion_reports.items()
        },
    }
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE runs SET finished_at = ?, total_payments = ?, ai_enabled = ?, metrics_json = ?,
                               status = ?, error = NULL WHERE run_id = ?""",
            (_now_iso(), n, int(run_ai.enabled), db.dumps(metrics), C.RUN_COMPLETED, run_id),
        )

    return metrics


def _persist(conn, run_id, payment, status, category, matched_ref, confidence, method, ai_used,
             reason, evidence, invoice_id, invoice_status, proc_ms=None) -> None:
    cur = conn.execute(
        """INSERT INTO decisions
           (run_id, payment_id, status, category, matched_bank_ref, confidence, method, ai_used,
            reason, invoice_id, invoice_status, evidence_json, processing_ms, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, payment["payment_id"], status, category, matched_ref, confidence, method, int(ai_used),
         reason, invoice_id, invoice_status, db.dumps(evidence), proc_ms, _now_iso()),
    )
    decision_id = cur.lastrowid

    actor = {"validation": "validator", "rule": "rule_engine", "ai": "ai_assisted"}[method]
    if method == "ai" and status == C.STATUS_EXCEPTION:
        actor = "policy_guardrail" if category == C.CAT_UNSUPPORTED_AI else "ai_assisted"
    conn.execute(
        """INSERT INTO audit_log
           (run_id, payment_id, decision_id, actor, status, category, ai_used, confidence, reason,
            evidence_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, payment["payment_id"], decision_id, actor, status, category, int(ai_used), confidence,
         reason, db.dumps(evidence), _now_iso()),
    )

    if status == C.STATUS_EXCEPTION:
        conn.execute(
            """INSERT INTO exceptions (run_id, payment_id, decision_id, category, suggested_action, created_at)
               VALUES (?,?,?,?,?,?)""",
            (run_id, payment["payment_id"], decision_id, category,
             SUGGESTED_ACTIONS.get(category, "Review manually."), _now_iso()),
        )

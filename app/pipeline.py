"""Reconciliation pipeline orchestrator.

Ties ingestion -> candidate generation -> deterministic scoring -> AI
escalation -> policy verification -> invoice corroboration -> persistence
together into a single, timed, auditable batch run.

This is the only module that writes decisions/audit_log/exceptions rows --
every other module is a pure function operating on in-memory data.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import constants as C
from app import db
from app.ai_reasoning import reason_about_candidates
from app.candidates import generate_candidates
from app.exceptions import build_exception_detail
from app.ingestion import ingest_all
from app.policy import apply_ai_policy
from app.scoring import NEEDS_AI, decide_deterministic
from config import THRESHOLDS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _corroborate_invoice(payment: dict, invoices_by_order: dict[str, dict], thresholds) -> tuple[dict | None, str]:
    inv = invoices_by_order.get(payment.get("order_id"))
    if inv is None:
        return None, C.INVOICE_NOT_FOUND
    p_amount = payment.get("amount")
    i_amount = inv.get("amount")
    if p_amount is None or i_amount is None:
        return inv, C.INVOICE_FOUND_MISMATCH
    diff_pct = abs(i_amount - p_amount) / p_amount if p_amount else 1.0
    if diff_pct <= thresholds.invoice_amount_tolerance_pct:
        return inv, C.INVOICE_FOUND_CONSISTENT
    return inv, C.INVOICE_FOUND_MISMATCH


def run_reconciliation(raw_dir: Path | None = None) -> dict:
    """Ingest the current raw dataset and run the full reconciliation batch.

    Returns a summary dict with counts, timing, and throughput. Full
    per-record results are persisted to SQLite (decisions/audit_log/exceptions).
    """
    from config import RAW_DIR

    raw_dir = raw_dir or RAW_DIR
    run_id = uuid.uuid4().hex[:12]
    started_at = _now_iso()
    t_start = time.perf_counter()

    ingestion_reports = ingest_all(raw_dir)

    with db.get_conn() as conn:
        payments = [dict(r) for r in conn.execute("SELECT * FROM payments").fetchall()]
        bank_rows = [dict(r) for r in conn.execute("SELECT * FROM bank_settlements").fetchall()]
        invoices = [dict(r) for r in conn.execute("SELECT * FROM invoices").fetchall()]
        invoices_by_order = {inv["order_id"]: inv for inv in invoices if inv.get("order_id")}

        status_counts = {C.STATUS_AUTO_MATCH: 0, C.STATUS_AI_ASSISTED_MATCH: 0, C.STATUS_EXCEPTION: 0}
        category_counts: dict[str, int] = {}
        ai_invocations = 0
        total_proc_ms = 0.0

        for payment in payments:
            t0 = time.perf_counter()

            if payment.get("validation_status") == "invalid":
                status, category, matched_ref = C.STATUS_EXCEPTION, C.CAT_MISSING_FIELDS, None
                method, ai_used, confidence = "validation", False, None
                reason = f"Record failed validation: {payment.get('validation_error')}"
                evidence = build_exception_detail(category, reason, [], [], [], None)
                _persist(conn, run_id, payment, status, category, matched_ref, confidence, method, ai_used,
                         reason, evidence, None, None)
                status_counts[status] += 1
                category_counts[category] = category_counts.get(category, 0) + 1
                total_proc_ms += (time.perf_counter() - t0) * 1000
                continue

            candidates = generate_candidates(payment, bank_rows, THRESHOLDS)
            outcome = decide_deterministic(candidates, THRESHOLDS)

            ai_used = False
            ai_evidence = None
            if outcome.status == C.STATUS_AUTO_MATCH:
                status, category = C.STATUS_AUTO_MATCH, None
                matched_ref, confidence, method = outcome.matched.bank_ref, 100, "rule"
                reason = outcome.reason

            elif outcome.status == NEEDS_AI:
                ai_used = True
                ai_invocations += 1
                ai_result = reason_about_candidates(payment, outcome.ai_candidates)
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

            invoice, invoice_status = _corroborate_invoice(payment, invoices_by_order, THRESHOLDS)
            invoice_id = invoice["invoice_id"] if invoice else None

            if status == C.STATUS_EXCEPTION:
                evidence = build_exception_detail(
                    category, reason, outcome.considered, outcome.rejected, outcome.duplicate_refs, ai_evidence,
                )
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

            status_counts[status] += 1
            if category:
                category_counts[category] = category_counts.get(category, 0) + 1

        elapsed = time.perf_counter() - t_start
        n = len(payments)
        metrics = {
            "run_id": run_id,
            "total_payments": n,
            "status_counts": status_counts,
            "category_counts": category_counts,
            "ai_invocations": ai_invocations,
            "ai_enabled": bool(__import__("config").AI_ENABLED),
            "total_processing_seconds": round(elapsed, 3),
            "throughput_per_second": round(n / elapsed, 2) if elapsed > 0 else None,
            "avg_processing_ms_per_record": round(total_proc_ms / n, 3) if n else None,
            "ingestion_reports": {
                k: {"rows_read": v.rows_read, "rows_valid": v.rows_valid, "rows_invalid": v.rows_invalid, "errors": v.errors}
                for k, v in ingestion_reports.items()
            },
        }
        conn.execute(
            "INSERT INTO runs (run_id, started_at, finished_at, total_payments, ai_enabled, metrics_json) VALUES (?,?,?,?,?,?)",
            (run_id, started_at, _now_iso(), n, int(metrics["ai_enabled"]), db.dumps(metrics)),
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
        from app.exceptions import SUGGESTED_ACTIONS
        conn.execute(
            """INSERT INTO exceptions (run_id, payment_id, decision_id, category, suggested_action, created_at)
               VALUES (?,?,?,?,?,?)""",
            (run_id, payment["payment_id"], decision_id, category,
             SUGGESTED_ACTIONS.get(category, "Review manually."), _now_iso()),
        )

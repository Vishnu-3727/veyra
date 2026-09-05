"""Naive baseline for comparison -- deliberately what a "just fuzzy-match
everything" system would do, with none of this project's safety guardrails:
no reference matching, no name evidence, no duplicate detection, no
ambiguity detection, no AI, no policy caps. It always commits to its single
closest-amount candidate within the settlement window, confidently, even
when that candidate is wrong or no real candidate exists.

This exists purely to make the value of the guardrail architecture
measurable rather than asserted: same dataset, same ground truth, same
scoring function (`app.evaluation.score_decisions`) -- the only variable is
whether evidence-gating is applied before committing to a match.

"Same dataset" is enforced, not assumed. The baseline is computed from a RUN's own source
snapshots (`run_payments` / `run_bank_settlements` / `run_ground_truth`), so selecting an older
run in the dashboard compares Veyra's decisions for that run against a naive matcher over the
exact same records. Reading the current on-disk CSVs instead (the old behavior) meant that after
regenerating the dataset the UI could show "Veyra on dataset A vs. naive baseline on dataset B"
without saying so -- an invalid comparison presented as the headline proof.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Optional

from app import constants as C
from app import db
from app.evaluation import load_ground_truth, load_run_ground_truth, score_decisions
from app.normalization import parse_amount, parse_date

_DESCRIPTION = (
    "Naive baseline: always commits to the closest-amount bank record within the "
    "settlement window. No reference matching, no name evidence, no duplicate "
    "detection, no ambiguity detection, no AI, no policy caps -- confidently wrong "
    "as often as it is confidently right."
)


def _read_rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _match_naively(payments: list[dict], banks: list[dict], settlement_window_days: int) -> dict[str, dict]:
    bank_parsed = []
    for b in banks:
        amount = parse_amount(b.get("amount"))
        date = parse_date(b.get("settlement_date"))
        if amount is not None and date is not None:
            bank_parsed.append((b["bank_ref"], amount, date))
    # Sorted so the tie-break below is a property of the DATA, not of the order rows happened to
    # arrive in. Without this, the same dataset scores differently depending on whether the rows
    # came from the CSV (generator's shuffled order) or from a run snapshot (bank_ref order):
    # equidistant candidates are extremely common in this naive scheme, and whichever row was
    # seen first won. That made the headline comparison irreproducible for no good reason.
    bank_parsed.sort(key=lambda r: r[0])

    decisions: dict[str, dict] = {}
    for p in payments:
        pid = p.get("payment_id")
        if not pid:
            continue
        amount = parse_amount(p.get("amount"))
        pdate = parse_date(p.get("created_at"))
        if amount is None or pdate is None:
            # A naive system without a validation layer would still try to match
            # on whatever partial data it has; with nothing usable, it skips.
            decisions[pid] = {"status": C.STATUS_EXCEPTION, "matched_bank_ref": None}
            continue

        # Closest amount match within the settlement window, full stop -- no reference check, no
        # name check, no "is this actually ambiguous" check. Ties go to the lowest bank_ref: a
        # naive matcher has no basis for choosing, and pretending otherwise would just hide the
        # ambiguity this baseline exists to illustrate.
        best_ref, best_diff = None, None
        for bank_ref, b_amount, b_date in bank_parsed:
            date_diff = (b_date - pdate).days
            if 0 <= date_diff <= settlement_window_days:
                diff = abs(b_amount - amount)
                if best_diff is None or diff < best_diff:
                    best_diff, best_ref = diff, bank_ref

        if best_ref is not None:
            decisions[pid] = {"status": C.STATUS_AUTO_MATCH, "matched_bank_ref": best_ref}
        else:
            decisions[pid] = {"status": C.STATUS_EXCEPTION, "matched_bank_ref": None}
    return decisions


def _finish(decisions: dict[str, dict], gt: dict[str, dict], payments_count: int, t0: float,
            run_id: Optional[str], source: str) -> dict:
    elapsed = time.perf_counter() - t0
    result = score_decisions(decisions, gt)
    result["run_id"] = run_id
    result["source"] = source
    result["total_processing_seconds"] = round(elapsed, 3)
    result["throughput_per_second"] = round(payments_count / elapsed, 2) if elapsed > 0 else None
    result["description"] = _DESCRIPTION
    return result


def compute_run_baseline(run_id: str, settlement_window_days: int = 7) -> Optional[dict]:
    """Baseline over the source data a specific run processed.

    Returns None if the run has no source snapshot (a legacy run recorded before snapshots
    existed), so the caller can decide what to say rather than silently substituting the current
    dataset and calling it the same comparison.
    """
    t0 = time.perf_counter()
    with db.get_conn() as conn:
        payments = [dict(r) for r in conn.execute(
            "SELECT * FROM run_payments WHERE run_id = ?", (run_id,)).fetchall()]
        banks = [dict(r) for r in conn.execute(
            "SELECT * FROM run_bank_settlements WHERE run_id = ?", (run_id,)).fetchall()]
    if not payments:
        return None
    gt = load_run_ground_truth(run_id)
    if gt is None:
        return None

    decisions = _match_naively(payments, banks, settlement_window_days)
    return _finish(decisions, gt, len(payments), t0, run_id, "run_snapshot")


def compute_naive_baseline(raw_dir: Path, settlement_window_days: int = 7,
                           run_id: Optional[str] = None) -> dict:
    """Baseline over the CURRENT on-disk dataset.

    Only used when no run snapshot is available (no runs yet, or a legacy run). The returned
    `source` field says so explicitly -- the caller/UI must not present it as a run-scoped
    comparison.
    """
    t0 = time.perf_counter()
    payments = _read_rows(raw_dir / "payments.csv")
    banks = _read_rows(raw_dir / "bank_settlements.csv")
    gt = load_ground_truth(raw_dir)
    decisions = _match_naively(payments, banks, settlement_window_days)
    return _finish(decisions, gt, len(payments), t0, run_id, "current_raw_file_fallback")

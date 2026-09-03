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
"""
from __future__ import annotations

import csv
import time
from pathlib import Path

from app import constants as C
from app.evaluation import load_ground_truth, score_decisions
from app.normalization import parse_amount, parse_date


def _read_rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def compute_naive_baseline(raw_dir: Path, settlement_window_days: int = 7) -> dict:
    t0 = time.perf_counter()
    payments = _read_rows(raw_dir / "payments.csv")
    banks = _read_rows(raw_dir / "bank_settlements.csv")
    gt = load_ground_truth(raw_dir)

    bank_parsed = []
    for b in banks:
        amount = parse_amount(b.get("amount"))
        date = parse_date(b.get("settlement_date"))
        if amount is not None and date is not None:
            bank_parsed.append((b["bank_ref"], amount, date))

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

        # Closest amount match within the settlement window, full stop -- no
        # reference check, no name check, no "is this actually ambiguous" check.
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

    elapsed = time.perf_counter() - t0
    result = score_decisions(decisions, gt)
    result["total_processing_seconds"] = round(elapsed, 3)
    result["throughput_per_second"] = round(len(payments) / elapsed, 2) if elapsed > 0 else None
    result["description"] = (
        "Naive baseline: always commits to the closest-amount bank record within the "
        "settlement window. No reference matching, no name evidence, no duplicate "
        "detection, no ambiguity detection, no AI, no policy caps -- confidently wrong "
        "as often as it is confidently right."
    )
    return result

"""Evaluation against known ground truth.

Ground truth is NEVER read by the reconciliation engine itself -- only here,
after the fact, to measure whether the system's decisions were actually
correct and, more importantly, actually *safe*.

Core safety framing: a false automatic match is worse than an unresolved
case. So every ground-truth payment is classified along two axes:
  - is_safely_resolvable (from the dataset generator): could ANY correct
    answer be determined from the evidence at all?
  - what the system actually did: matched (auto or AI-assisted) or excepted.

This yields five mutually exclusive outcomes per payment:
  CORRECT_AUTO         resolvable, matched, and matched the right record
  INCORRECT_AUTO        resolvable, matched, but matched the WRONG record (false match)
  MISSED_OPPORTUNITY    resolvable, but the system excepted it instead (safe, but a coverage loss)
  UNSAFE_AUTO            NOT resolvable, yet the system matched anyway (safety violation)
  CORRECTLY_ESCALATED   NOT resolvable, and the system correctly excepted it
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from app import constants as C
from app import db


def load_ground_truth(raw_dir: Path) -> dict[str, dict]:
    path = raw_dir / "ground_truth.csv"
    gt = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            row["is_safely_resolvable"] = row["is_safely_resolvable"].strip().lower() == "true"
            row["true_bank_refs"] = set(filter(None, row["true_bank_ref"].split("|")))
            gt[row["payment_id"]] = row
    return gt


def _run_has_ground_truth_snapshot(run_id: str) -> bool:
    """Whether this run recorded a ground-truth snapshot at all.

    Row COUNT cannot answer this: a legitimate run over a dataset with an empty ground_truth.csv
    snapshots zero rows, which is NOT the same as a legacy run that never snapshotted anything.
    Treating "zero rows" as "no snapshot" would silently score such a run against whatever
    ground_truth.csv happens to be on disk now. The run row carries the flag explicitly.
    """
    with db.get_conn() as conn:
        row = conn.execute("SELECT ground_truth_snapshotted FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is not None and row["ground_truth_snapshotted"]:
            return True
        # Belt-and-braces for a run whose flag predates this column but whose snapshot rows exist.
        return bool(conn.execute(
            "SELECT 1 FROM run_ground_truth WHERE run_id = ? LIMIT 1", (run_id,)).fetchone())


def load_run_ground_truth(run_id: str) -> Optional[dict[str, dict]]:
    """Ground truth as it was snapshotted at the time this run executed (see
    `pipeline._snapshot_ground_truth`). Returns None only when the run has NO snapshot (e.g. a
    run persisted before per-run snapshots existed), in which case the caller must fall back
    explicitly and label the result as non-reproducible."""
    if not _run_has_ground_truth_snapshot(run_id):
        return None
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM run_ground_truth WHERE run_id = ?", (run_id,)).fetchall()
    gt = {}
    for r in rows:
        row = dict(r)
        row["is_safely_resolvable"] = bool(row["is_safely_resolvable"])
        row["true_bank_refs"] = set(filter(None, (row["true_bank_ref"] or "").split("|")))
        gt[row["payment_id"]] = row
    return gt


def score_decisions(decisions: dict[str, dict], gt: dict[str, dict]) -> dict:
    """Score an arbitrary {payment_id: {"status": ..., "matched_bank_ref": ...}}
    mapping against ground truth. Used both for the real engine's decisions
    (via `evaluate`) and for the naive baseline comparison (`app/baseline.py`)
    so both are scored with identical, unbiased logic.
    """
    outcomes: dict[str, int] = {
        "CORRECT_AUTO": 0, "INCORRECT_AUTO": 0, "MISSED_OPPORTUNITY": 0,
        "UNSAFE_AUTO": 0, "CORRECTLY_ESCALATED": 0,
    }
    per_case_type: dict[str, dict[str, int]] = {}
    incorrect_examples: list[dict] = []
    unsafe_examples: list[dict] = []
    joined = 0

    for payment_id, g in gt.items():
        d = decisions.get(payment_id)
        if d is None:
            continue  # row dropped at ingestion (e.g. missing payment_id) -- excluded from eval, not fabricated
        joined += 1
        matched = d["status"] in C.MATCHED_STATUSES
        resolvable = g["is_safely_resolvable"]

        if resolvable and matched:
            if g["true_bank_refs"] and d["matched_bank_ref"] in g["true_bank_refs"]:
                outcome = "CORRECT_AUTO"
            else:
                outcome = "INCORRECT_AUTO"
                incorrect_examples.append({
                    "payment_id": payment_id, "matched_bank_ref": d["matched_bank_ref"],
                    "true_bank_ref": g["true_bank_ref"], "case_type": g["case_type"],
                })
        elif resolvable and not matched:
            outcome = "MISSED_OPPORTUNITY"
        elif not resolvable and matched:
            outcome = "UNSAFE_AUTO"
            unsafe_examples.append({
                "payment_id": payment_id, "matched_bank_ref": d["matched_bank_ref"],
                "case_type": g["case_type"], "status": d["status"],
            })
        else:
            outcome = "CORRECTLY_ESCALATED"

        outcomes[outcome] += 1
        ct = g["case_type"]
        per_case_type.setdefault(ct, {"total": 0, **{k: 0 for k in outcomes}})
        per_case_type[ct]["total"] += 1
        per_case_type[ct][outcome] += 1

    automated = outcomes["CORRECT_AUTO"] + outcomes["INCORRECT_AUTO"] + outcomes["UNSAFE_AUTO"]
    resolvable_total = outcomes["CORRECT_AUTO"] + outcomes["INCORRECT_AUTO"] + outcomes["MISSED_OPPORTUNITY"]
    unresolvable_total = outcomes["UNSAFE_AUTO"] + outcomes["CORRECTLY_ESCALATED"]

    return {
        "source_records": len(gt),
        "joined_records": joined,
        "dropped_records": len(gt) - joined,
        "outcomes": outcomes,
        "automation_precision": round(outcomes["CORRECT_AUTO"] / automated, 4) if automated else None,
        "coverage_recall": round(outcomes["CORRECT_AUTO"] / resolvable_total, 4) if resolvable_total else None,
        "safety_rate": round(outcomes["CORRECTLY_ESCALATED"] / unresolvable_total, 4) if unresolvable_total else None,
        "false_match_rate": round((outcomes["INCORRECT_AUTO"] + outcomes["UNSAFE_AUTO"]) / automated, 4) if automated else 0.0,
        "unresolved_rate": round((joined - automated) / joined, 4) if joined else None,
        "automation_rate": round(automated / joined, 4) if joined else None,
        "resolvable_total": resolvable_total,
        "unresolvable_total": unresolvable_total,
        "per_case_type": per_case_type,
        "incorrect_auto_examples": incorrect_examples,
        "unsafe_auto_examples": unsafe_examples,
    }


def evaluate(run_id: str, raw_dir: Path) -> dict:
    gt = load_run_ground_truth(run_id)
    ground_truth_source = "run_snapshot"
    if gt is None:
        # No snapshot for this run (e.g. it predates the run_ground_truth table) -- fall back to
        # whatever ground_truth.csv currently sits in raw_dir. This can drift from what the run
        # actually saw if the dataset has since been regenerated; flag that explicitly rather than
        # presenting the numbers as if they were still tied to this run.
        gt = load_ground_truth(raw_dir)
        ground_truth_source = "current_raw_file_fallback"
    with db.get_conn() as conn:
        decisions = {
            r["payment_id"]: dict(r)
            for r in conn.execute("SELECT * FROM decisions WHERE run_id = ?", (run_id,)).fetchall()
        }
    return {"run_id": run_id, "ground_truth_source": ground_truth_source, **score_decisions(decisions, gt)}

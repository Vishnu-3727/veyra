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

from app import constants as C
from app import db


def _load_ground_truth(raw_dir: Path) -> dict[str, dict]:
    path = raw_dir / "ground_truth.csv"
    gt = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            row["is_safely_resolvable"] = row["is_safely_resolvable"].strip().lower() == "true"
            row["true_bank_refs"] = set(filter(None, row["true_bank_ref"].split("|")))
            gt[row["payment_id"]] = row
    return gt


def evaluate(run_id: str, raw_dir: Path) -> dict:
    gt = _load_ground_truth(raw_dir)

    with db.get_conn() as conn:
        decisions = {
            r["payment_id"]: dict(r)
            for r in conn.execute("SELECT * FROM decisions WHERE run_id = ?", (run_id,)).fetchall()
        }

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

    metrics = {
        "run_id": run_id,
        "joined_records": joined,
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
    return metrics

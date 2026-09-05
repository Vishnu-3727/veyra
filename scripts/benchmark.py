#!/usr/bin/env python3
"""Reproducible performance benchmark for the reconciliation engine.

Measures a full end-to-end batch (ingestion -> candidate generation -> deterministic scoring ->
persistence) at several dataset sizes, against a throwaway database and a throwaway dataset, so
the numbers quoted in the README can be regenerated on any machine rather than trusted.

    python scripts/benchmark.py                       # 750 1500 3000 5000
    python scripts/benchmark.py --sizes 750 20000
    python scripts/benchmark.py --oracle              # also time the brute-force candidate
                                                      # generator, for the O(P x B) comparison

AI is not involved: with no API key configured, ambiguous cases fail closed to exceptions, so
timings measure the deterministic engine only (no network variance).
"""
from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


def _rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        out = []
        for r in csv.DictReader(f):
            r["amount"] = float(r["amount"]) if r["amount"] else None
            out.append(r)
        return out


def bench(size: int, oracle: bool) -> dict:
    from app import db
    from app.candidates import BankCandidateIndex, _generate_candidates_bruteforce, generate_candidates
    from app.generate_dataset import generate
    from app.pipeline import run_reconciliation
    from config import THRESHOLDS

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        raw = tmp_path / "raw"
        raw.mkdir()
        config.DB_PATH = tmp_path / "bench.db"
        db.init_db()

        t0 = time.perf_counter()
        summary = generate(42, size, raw)
        gen_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        metrics = run_reconciliation(raw)
        run_s = time.perf_counter() - t0

        payments, banks = _rows(raw / "payments.csv"), _rows(raw / "bank_settlements.csv")

        t0 = time.perf_counter()
        index = BankCandidateIndex(banks)
        for p in payments:
            generate_candidates(p, index, THRESHOLDS)
        indexed_s = time.perf_counter() - t0

        oracle_s = None
        if oracle:
            t0 = time.perf_counter()
            for p in payments:
                _generate_candidates_bruteforce(p, banks, THRESHOLDS)
            oracle_s = time.perf_counter() - t0

    return {
        "payments": size,
        "bank_rows": summary["bank_settlements"],
        "generate_s": round(gen_s, 2),
        "full_run_s": round(run_s, 2),
        "records_per_s": round(size / run_s, 1) if run_s else None,
        "candidates_indexed_s": round(indexed_s, 2),
        "candidates_bruteforce_s": round(oracle_s, 2) if oracle_s is not None else None,
        "speedup": round(oracle_s / indexed_s, 1) if oracle_s else None,
        "status_counts": metrics["status_counts"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", type=int, nargs="+", default=[750, 1500, 3000, 5000])
    ap.add_argument("--oracle", action="store_true", help="also time the brute-force candidate generator")
    args = ap.parse_args()

    header = f"{'payments':>9} {'bank':>7} {'full run s':>11} {'rec/s':>8} {'cand idx s':>11}"
    if args.oracle:
        header += f" {'cand brute s':>13} {'speedup':>8}"
    print(header)
    for size in args.sizes:
        r = bench(size, args.oracle)
        line = (f"{r['payments']:>9} {r['bank_rows']:>7} {r['full_run_s']:>11} "
                f"{r['records_per_s']:>8} {r['candidates_indexed_s']:>11}")
        if args.oracle:
            line += f" {r['candidates_bruteforce_s']:>13} {str(r['speedup']) + 'x':>8}"
        print(line, flush=True)
        print(f"{'':>9} decisions: {r['status_counts']}", flush=True)


if __name__ == "__main__":
    main()

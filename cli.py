#!/usr/bin/env python3
"""Command-line interface -- run the full pipeline without the API/dashboard.

Useful for CI, judges who want raw output, or debugging. Same underlying
functions the API calls; no duplicated logic.

Usage:
    python cli.py generate --seed 42 --size 750
    python cli.py run
    python cli.py evaluate [--run-id RUN_ID]
"""
from __future__ import annotations

import argparse
import json
import sys

import config


def cmd_generate(args) -> None:
    from app.generate_dataset import generate

    summary = generate(args.seed, args.size, config.RAW_DIR)
    print(json.dumps(summary, indent=2))


def cmd_run(args) -> None:
    from app.pipeline import run_reconciliation

    metrics = run_reconciliation(config.RAW_DIR)
    print(json.dumps(metrics, indent=2))


def cmd_evaluate(args) -> None:
    from app import db
    from app.evaluation import evaluate

    run_id = args.run_id or db.latest_run_id()
    if not run_id:
        print("No runs found. Run `python cli.py run` first.", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(evaluate(run_id, config.RAW_DIR), indent=2))


def cmd_baseline(args) -> None:
    from app.baseline import compute_naive_baseline

    print(json.dumps(compute_naive_baseline(config.RAW_DIR), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="Generate the synthetic dataset")
    p_gen.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    p_gen.add_argument("--size", type=int, default=config.DATASET_SIZE)
    p_gen.set_defaults(func=cmd_generate)

    p_run = sub.add_parser("run", help="Ingest + reconcile the current dataset")
    p_run.set_defaults(func=cmd_run)

    p_eval = sub.add_parser("evaluate", help="Evaluate a run against ground truth")
    p_eval.add_argument("--run-id", default=None, help="Defaults to the most recent run")
    p_eval.set_defaults(func=cmd_evaluate)

    p_base = sub.add_parser("baseline", help="Compute the naive baseline comparison")
    p_base.set_defaults(func=cmd_baseline)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

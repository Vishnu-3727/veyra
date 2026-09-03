"""FastAPI service exposing the reconciliation engine.

Thin HTTP layer over app/pipeline.py, app/evaluation.py, and the SQLite
tables in app/db.py. No business logic lives here -- only request handling,
serialization, and input validation at the boundary.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from app import constants as C
from app import db
from app.evaluation import evaluate
from app.pipeline import run_reconciliation

app = FastAPI(title="AI Finance Controller", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def _row(r) -> dict:
    d = dict(r)
    for key in ("evidence_json",):
        if key in d and d[key]:
            try:
                d[key.replace("_json", "")] = json.loads(d.pop(key))
            except (json.JSONDecodeError, TypeError):
                pass
    return d


@app.get("/health")
def health():
    return {"status": "ok", "ai_enabled": config.AI_ENABLED, "llm_model": config.LLM_MODEL if config.AI_ENABLED else None}


@app.post("/dataset/generate")
def generate_dataset(seed: int = config.RANDOM_SEED, size: int = config.DATASET_SIZE):
    if size < 1 or size > 20000:
        raise HTTPException(400, "size must be between 1 and 20000")
    sys.path.insert(0, str(config.BASE_DIR / "data"))
    from generate_dataset import generate  # local import to avoid module name clashes at startup

    summary = generate(seed, size, config.RAW_DIR)
    return summary


@app.post("/reconcile/run")
def reconcile_run():
    if not (config.RAW_DIR / "payments.csv").exists():
        raise HTTPException(400, "No dataset found. Call POST /dataset/generate first.")
    try:
        metrics = run_reconciliation(config.RAW_DIR)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    return metrics


@app.get("/runs")
def list_runs(limit: int = 20):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT run_id, started_at, finished_at, total_payments, ai_enabled FROM runs "
            "ORDER BY started_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/runs/latest")
def latest_run():
    with db.get_conn() as conn:
        row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if not row:
        raise HTTPException(404, "No runs yet. Call POST /reconcile/run first.")
    return {"run_id": row["run_id"]}


@app.get("/runs/{run_id}")
def run_detail(run_id: str):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"Run {run_id} not found")
    d = dict(row)
    d["metrics"] = json.loads(d.pop("metrics_json"))
    return d


@app.get("/runs/{run_id}/evaluation")
def run_evaluation(run_id: str):
    with db.get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not exists:
        raise HTTPException(404, f"Run {run_id} not found")
    return evaluate(run_id, config.RAW_DIR)


@app.get("/baseline")
def baseline_comparison():
    """Naive closest-amount-in-window matcher, scored the same way, for a
    direct, quantified comparison against the guardrailed engine."""
    if not (config.RAW_DIR / "payments.csv").exists():
        raise HTTPException(400, "No dataset found. Call POST /dataset/generate first.")
    from app.baseline import compute_naive_baseline

    return compute_naive_baseline(config.RAW_DIR)


@app.get("/decisions")
def list_decisions(
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    run_id = run_id or _resolve_latest_run_id()
    query = (
        "SELECT d.*, p.customer_name, p.amount, p.order_id, p.created_at as payment_created_at "
        "FROM decisions d JOIN payments p ON p.payment_id = d.payment_id WHERE d.run_id = ?"
    )
    params: list = [run_id]
    if status:
        query += " AND d.status = ?"
        params.append(status)
    if category:
        query += " AND d.category = ?"
        params.append(category)
    query += " ORDER BY d.id LIMIT ? OFFSET ?"
    params += [limit, offset]
    with db.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) c FROM decisions WHERE run_id = ?" + (" AND status = ?" if status else "") + (" AND category = ?" if category else ""),
            [run_id] + ([status] if status else []) + ([category] if category else []),
        ).fetchone()["c"]
    return {"run_id": run_id, "total": total, "results": [_row(r) for r in rows]}


@app.get("/decisions/{payment_id}")
def decision_detail(payment_id: str, run_id: Optional[str] = None):
    run_id = run_id or _resolve_latest_run_id()
    with db.get_conn() as conn:
        decision = conn.execute(
            "SELECT * FROM decisions WHERE payment_id = ? AND run_id = ?", (payment_id, run_id),
        ).fetchone()
        payment = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)).fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_log WHERE payment_id = ? AND run_id = ? ORDER BY id", (payment_id, run_id),
        ).fetchall()
    if not decision:
        raise HTTPException(404, f"No decision for payment {payment_id} in run {run_id}")
    return {
        "payment": dict(payment) if payment else None,
        "decision": _row(decision),
        "audit_trail": [_row(r) for r in audit],
    }


@app.get("/exceptions")
def list_exceptions(run_id: Optional[str] = None, category: Optional[str] = None, limit: int = 200):
    run_id = run_id or _resolve_latest_run_id()
    query = (
        "SELECT e.*, d.reason, d.evidence_json, p.customer_name, p.amount, p.order_id "
        "FROM exceptions e "
        "JOIN decisions d ON d.id = e.decision_id "
        "JOIN payments p ON p.payment_id = e.payment_id "
        "WHERE e.run_id = ?"
    )
    params: list = [run_id]
    if category:
        query += " AND e.category = ?"
        params.append(category)
    query += " ORDER BY e.id LIMIT ?"
    params.append(limit)
    with db.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"run_id": run_id, "results": [_row(r) for r in rows]}


@app.get("/audit")
def list_audit(run_id: Optional[str] = None, payment_id: Optional[str] = None, limit: int = 200):
    run_id = run_id or _resolve_latest_run_id()
    query = "SELECT * FROM audit_log WHERE run_id = ?"
    params: list = [run_id]
    if payment_id:
        query += " AND payment_id = ?"
        params.append(payment_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"run_id": run_id, "results": [_row(r) for r in rows]}


def _resolve_latest_run_id() -> str:
    with db.get_conn() as conn:
        row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if not row:
        raise HTTPException(404, "No runs yet. Call POST /reconcile/run first.")
    return row["run_id"]


@app.on_event("startup")
def _startup():
    db.init_db()

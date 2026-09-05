"""FastAPI service exposing the reconciliation engine.

Thin HTTP layer over app/pipeline.py, app/evaluation.py, and the SQLite
tables in app/db.py. No business logic lives here -- only request handling,
serialization, and input validation at the boundary.
"""
from __future__ import annotations

import hmac
import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import config
from app import constants as C
from app import db
from app import settings as llm_settings
from app.evaluation import evaluate
from app.generate_dataset import generate
from app.pipeline import run_reconciliation


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Schema creation + column migrations run once, before the first request is served.
    # (`@app.on_event("startup")` is deprecated in current FastAPI; lifespan is the supported
    # equivalent and behaves identically here.)
    db.init_db()
    yield


app = FastAPI(
    title="Veyra — AI Finance Controller", version="1.0", lifespan=_lifespan,
    # The interactive docs publish every endpoint and parameter; the dashboard never uses them.
    docs_url="/docs" if config.API_ENABLE_DOCS else None,
    redoc_url="/redoc" if config.API_ENABLE_DOCS else None,
    openapi_url="/openapi.json" if config.API_ENABLE_DOCS else None,
)

# Dataset generation and reconciliation both operate on the same batch state: generation
# rewrites data/raw/*.csv, reconciliation ingests those files into the source tables and reads
# them for a whole run. Running them concurrently could reconcile a half-written dataset, or
# regenerate the dataset underneath an in-flight run (mixing two datasets inside one run_id),
# so they share ONE process-level operation lock and the loser gets 409 instead of corrupt state.
_dataset_operation_lock = threading.Lock()

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_allowed(request: Request) -> bool:
    """Whether a state-changing request may proceed, based on its `Origin` header.

    A browser always sends `Origin` on a cross-site POST; a non-browser client (curl, the CLI,
    a server-side integration) sends none. So "no Origin" is allowed and "an Origin we do not
    serve the dashboard from" is refused -- which is precisely the CSRF case, and the one thing
    CORS cannot stop on its own (CORS hides the *response*; the side effect already happened).
    """
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin.strip().rstrip("/") in config.CORS_ALLOWED_ORIGINS


async def _guard(request: Request, call_next):
    """Optional shared-secret auth plus cross-site write protection.

    Auth is disabled (a no-op) unless API_AUTH_TOKEN is set -- the default localhost demo is
    unaffected. /health is always exempt so run.sh's own health-check keeps working without
    knowing the token. The token is compared with `hmac.compare_digest`, so a wrong guess costs
    the same time regardless of how many leading bytes were right.

    Registered BEFORE CORSMiddleware on purpose: Starlette wraps middleware in reverse
    registration order, so the last one registered is the outermost. Registering this one last
    put it outside CORS, and every 401 it returned then lacked `Access-Control-Allow-Origin` --
    the browser turned a legible 401 into an opaque network error, so the dashboard reported
    "cannot reach API" and its token prompt never appeared. Enabling API_AUTH_TOKEN was
    therefore impossible from a browser. Now CORS is outermost and decorates these responses.
    """
    if request.method == "OPTIONS":
        # A CORS preflight carries no credentials by definition; CORSMiddleware answers it.
        return await call_next(request)
    if config.API_AUTH_TOKEN and request.url.path != "/health":
        auth_header = request.headers.get("authorization", "")
        bearer = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
        supplied = request.headers.get("x-api-token") or bearer
        if not hmac.compare_digest(supplied, config.API_AUTH_TOKEN):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    if request.method in _MUTATING_METHODS and not _origin_allowed(request):
        return JSONResponse(
            {"detail": "Cross-site request refused: this endpoint changes state and the request's "
                       "Origin is not an allowed dashboard origin."},
            status_code=403,
        )
    return await call_next(request)


app.middleware("http")(_guard)
# Registered last => outermost, so it decorates even the 401/403 above with CORS headers.
app.add_middleware(
    CORSMiddleware, allow_origins=config.CORS_ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"],
)


def _row(r) -> dict:
    """Row as a dict, with evidence_json decoded into an `evidence` object."""
    d = dict(r)
    if d.get("evidence_json"):
        try:
            d["evidence"] = json.loads(d.pop("evidence_json"))
        except (json.JSONDecodeError, TypeError):
            pass
    return d


@app.get("/health")
def health():
    """Liveness probe. Deliberately exempt from the API token so run.sh's own readiness check
    works without knowing it -- and therefore deliberately terse once a token IS configured:
    an unauthenticated caller has no business learning which provider/model holds the key it
    might try to steal. run.sh only reads the status code (`curl -sf ... >/dev/null`)."""
    if config.API_AUTH_TOKEN:
        return {"status": "ok"}
    s = llm_settings.get()
    return {"status": "ok", "ai_enabled": s.enabled, "llm_provider": s.provider if s.enabled else None,
            "llm_model": s.model if s.enabled else None}


@app.get("/meta")
def meta():
    """Shared vocabulary (status/category labels) so the frontend never
    hardcodes a second copy of text that could drift from app/constants.py."""
    return {
        "statuses": {
            C.STATUS_AUTO_MATCH: "Auto-matched (rule)",
            C.STATUS_AI_ASSISTED_MATCH: "AI-assisted match",
            C.STATUS_EXCEPTION: "Exception",
        },
        "category_labels": C.CATEGORY_LABELS,
        "thresholds": {
            "ai_confidence_threshold": config.THRESHOLDS.ai_confidence_threshold,
            "ai_hard_amount_mismatch_cap_pct": config.THRESHOLDS.ai_hard_amount_mismatch_cap_pct,
            "settlement_window_days": config.THRESHOLDS.settlement_window_days,
            "exact_amount_tolerance_pct": config.THRESHOLDS.exact_amount_tolerance_pct,
            "high_name_similarity": config.THRESHOLDS.high_name_similarity,
            "min_name_similarity_for_ai": config.THRESHOLDS.min_name_similarity_for_ai,
            "max_amount_mismatch_for_ai_pct": config.THRESHOLDS.max_amount_mismatch_for_ai_pct,
        },
    }


@app.get("/settings")
def get_settings():
    """Current LLM provider configuration (never includes the raw API key)
    plus the curated provider presets the dashboard's Settings panel offers."""
    s = llm_settings.get()
    return {**s.public_dict(), "presets": llm_settings.PROVIDER_PRESETS}


class SettingsUpdate(BaseModel):
    """POSTed as a JSON body (not query params) so the API key never appears in a URL -- URLs are
    routinely logged by browsers, proxies, and monitoring tools; JSON request bodies are not."""
    provider: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None


@app.post("/settings")
def update_settings(payload: SettingsUpdate):
    """Switch LLM provider/model or set a new API key, live, with no restart.
    Omit `api_key` to keep the currently configured one (e.g. when only
    switching models within the same provider); pass an empty string to
    clear it.

    `base_url` is validated against the curated presets (see
    `app.settings.validate_api_base_url`), and ANY endpoint change drops the stored key unless
    a new one is supplied in the same request -- so this endpoint can never be used to redirect
    the configured credential to a caller-chosen host.
    """
    if payload.provider is not None and payload.provider not in llm_settings.PROVIDER_PRESETS:
        raise HTTPException(400, f"Unknown provider {payload.provider!r}. Valid: {list(llm_settings.PROVIDER_PRESETS)}")
    try:
        s = llm_settings.update(provider=payload.provider, api_key=payload.api_key,
                                base_url=payload.base_url, model=payload.model)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**s.public_dict(), "presets": llm_settings.PROVIDER_PRESETS}


@app.post("/dataset/generate")
def generate_dataset(seed: int = Query(config.RANDOM_SEED, ge=0, le=2**31 - 1),
                     size: int = Query(config.DATASET_SIZE, ge=1, le=config.MAX_DATASET_SIZE)):
    if not _dataset_operation_lock.acquire(blocking=False):
        raise HTTPException(409, "A dataset/reconciliation operation is already in progress. "
                                 "Wait for it to finish before regenerating the dataset.")
    try:
        return generate(seed, size, config.RAW_DIR)
    finally:
        _dataset_operation_lock.release()


@app.post("/reconcile/run")
def reconcile_run():
    if not (config.RAW_DIR / "payments.csv").exists():
        raise HTTPException(400, "No dataset found. Call POST /dataset/generate first.")
    if not _dataset_operation_lock.acquire(blocking=False):
        raise HTTPException(409, "A dataset/reconciliation operation is already in progress. "
                                 "Wait for it to finish before starting another run.")
    try:
        metrics = run_reconciliation(config.RAW_DIR)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    finally:
        _dataset_operation_lock.release()
    return metrics


@app.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=200)):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT run_id, started_at, finished_at, total_payments, ai_enabled, status, error, "
            "ai_provider, ai_model, dataset_fingerprint, dataset_seed, dataset_size FROM runs "
            "ORDER BY started_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/runs/latest")
def latest_run():
    return {"run_id": _resolve_latest_run_id()}


@app.get("/runs/{run_id}")
def run_detail(run_id: str):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"Run {run_id} not found")
    d = dict(row)
    raw_metrics = d.pop("metrics_json")
    try:
        d["metrics"] = json.loads(raw_metrics) if raw_metrics else {}
    except (json.JSONDecodeError, TypeError):
        d["metrics"] = {}
    return d


@app.get("/runs/{run_id}/evaluation")
def run_evaluation(run_id: str):
    with db.get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not exists:
        raise HTTPException(404, f"Run {run_id} not found")
    return evaluate(run_id, config.RAW_DIR)


@app.get("/baseline")
def baseline_comparison(run_id: Optional[str] = None):
    """Naive closest-amount-in-window matcher, scored the same way, for a direct, quantified
    comparison against the guardrailed engine.

    Run-scoped: the baseline is computed from the SAME run's source snapshots that produced the
    decisions it is compared against. Omitting `run_id` uses the latest COMPLETED run. Only when
    no snapshot exists (no runs yet, or a legacy run) does it fall back to the current on-disk
    dataset, and the response says so via `source` so the UI cannot present a cross-dataset
    comparison as apples-to-apples.
    """
    from app.baseline import compute_naive_baseline, compute_run_baseline

    requested = run_id
    run_id = run_id or db.latest_completed_run_id()
    if requested and not db.run_exists(requested):
        # An explicitly requested run that does not exist must not silently answer with the
        # current dataset's numbers stamped with the caller's run_id -- that is precisely the
        # cross-dataset comparison this endpoint exists to prevent.
        raise HTTPException(404, f"Run {requested} not found")
    if run_id:
        result = compute_run_baseline(run_id)
        if result is not None:
            return result
    # Fallback path: reads the mutable raw dataset, so hold the dataset operation lock to avoid
    # reading a dataset that is being regenerated underneath us. Non-blocking, exactly like
    # /dataset/generate and /reconcile/run: a sync endpoint that waits occupies one of the
    # bounded anyio worker threads for the whole wait, so repeated calls during a long run
    # could starve every other synchronous endpoint. Answer 409 and let the caller retry.
    if not (config.RAW_DIR / "payments.csv").exists():
        raise HTTPException(400, "No dataset found. Call POST /dataset/generate first.")
    if not _dataset_operation_lock.acquire(blocking=False):
        raise HTTPException(409, "A dataset/reconciliation operation is in progress; try again shortly.")
    try:
        return compute_naive_baseline(config.RAW_DIR, run_id=run_id)
    finally:
        _dataset_operation_lock.release()


# Payment metadata for a run comes from that run's snapshot (`run_payments`), falling back to the
# current `payments` table only for legacy runs recorded before snapshots existed. Joining the
# mutable current-batch table directly would make an older run's rows vanish (or, worse, show
# another dataset's payment) as soon as a new dataset is ingested.
_PAYMENT_JOIN = (
    " LEFT JOIN run_payments rp ON rp.run_id = d.run_id AND rp.payment_id = d.payment_id"
    " LEFT JOIN payments p ON p.payment_id = d.payment_id"
)
_PAYMENT_FIELDS = (
    " COALESCE(rp.customer_name, p.customer_name) AS customer_name,"
    " COALESCE(rp.amount, p.amount) AS amount,"
    " COALESCE(rp.order_id, p.order_id) AS order_id,"
    " COALESCE(rp.created_at, p.created_at) AS payment_created_at"
)


@app.get("/decisions")
def list_decisions(
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    run_id = run_id or _resolve_latest_run_id()
    query = "SELECT d.*," + _PAYMENT_FIELDS + " FROM decisions d" + _PAYMENT_JOIN + " WHERE d.run_id = ?"
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
        payment = conn.execute(
            "SELECT * FROM run_payments WHERE run_id = ? AND payment_id = ?", (run_id, payment_id),
        ).fetchone()
        if payment is None:  # legacy run without a snapshot
            payment = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)).fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_log WHERE payment_id = ? AND run_id = ? ORDER BY id", (payment_id, run_id),
        ).fetchall()
        exception_row = None
        if decision and decision["status"] == C.STATUS_EXCEPTION:
            exception_row = conn.execute(
                "SELECT * FROM exceptions WHERE decision_id = ?", (decision["id"],),
            ).fetchone()
    if not decision:
        raise HTTPException(404, f"No decision for payment {payment_id} in run {run_id}")
    return {
        "payment": dict(payment) if payment else None,
        "decision": _row(decision),
        "audit_trail": [_row(r) for r in audit],
        "exception": dict(exception_row) if exception_row else None,
    }


@app.get("/exceptions")
def list_exceptions(run_id: Optional[str] = None, category: Optional[str] = None,
                    limit: int = Query(200, ge=1, le=2000)):
    run_id = run_id or _resolve_latest_run_id()
    query = (
        "SELECT e.*, d.reason, d.evidence_json,"
        " COALESCE(rp.customer_name, p.customer_name) AS customer_name,"
        " COALESCE(rp.amount, p.amount) AS amount,"
        " COALESCE(rp.order_id, p.order_id) AS order_id "
        "FROM exceptions e "
        "JOIN decisions d ON d.id = e.decision_id "
        "LEFT JOIN run_payments rp ON rp.run_id = e.run_id AND rp.payment_id = e.payment_id "
        "LEFT JOIN payments p ON p.payment_id = e.payment_id "
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
        total = conn.execute(
            "SELECT COUNT(*) c FROM exceptions WHERE run_id = ?" + (" AND category = ?" if category else ""),
            [run_id] + ([category] if category else []),
        ).fetchone()["c"]
    # `total` lets the dashboard say "N of TOTAL shown" instead of implying a truncated list is
    # the complete exception list -- an honest exception queue is the point of the product.
    return {"run_id": run_id, "total": total, "results": [_row(r) for r in rows]}


@app.post("/exceptions/{exception_id}/resolve")
def resolve_exception(exception_id: int):
    """Mark an exception as reviewed by a human. Idempotent.

    This never touches the reconciliation decision itself -- it only records that a person
    looked at it, via the `resolved` flag that already existed in the schema. Marking reviewed
    is NOT approving a match: the engine's `status`/`matched_bank_ref` stay exactly as decided.
    The review is written to the audit log as its own `human_reviewer` event, so the trail reads
    ENGINE DECISION -> EXCEPTION -> HUMAN REVIEWED instead of a flag flipping with no provenance.

    Re-posting (a double-click, a retried request) must not append a second review event: one
    human review action is one provenance event, otherwise the audit history is inflated by UI
    noise. Already-reviewed exceptions return their existing state with `already_reviewed=true`.
    """
    reviewed_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM exceptions WHERE id = ?", (exception_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Exception {exception_id} not found")
        if row["resolved"]:
            return {**dict(row), "already_reviewed": True}
        conn.execute("UPDATE exceptions SET resolved = 1 WHERE id = ?", (exception_id,))
        conn.execute(
            """INSERT INTO audit_log
               (run_id, payment_id, decision_id, actor, status, category, ai_used, confidence,
                reason, evidence_json, exception_id, created_at)
               VALUES (?,?,?,?,?,?,0,NULL,?,?,?,?)""",
            (row["run_id"], row["payment_id"], row["decision_id"], "human_reviewer",
             C.STATUS_EXCEPTION_REVIEWED, row["category"],
             "Exception marked reviewed by operator. The engine's decision is unchanged; this "
             "records only that a human examined the case.",
             db.dumps({"exception_id": exception_id, "reviewed_at": reviewed_at,
                       "engine_decision_id": row["decision_id"]}),
             exception_id, reviewed_at),
        )
        updated = conn.execute("SELECT * FROM exceptions WHERE id = ?", (exception_id,)).fetchone()
    return {**dict(updated), "already_reviewed": False}


@app.get("/audit")
def list_audit(run_id: Optional[str] = None, payment_id: Optional[str] = None,
               limit: int = Query(200, ge=1, le=2000)):
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
        total = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE run_id = ?" + (" AND payment_id = ?" if payment_id else ""),
            [run_id] + ([payment_id] if payment_id else []),
        ).fetchone()["c"]
    return {"run_id": run_id, "total": total, "results": [_row(r) for r in rows]}


def _resolve_latest_run_id() -> str:
    run_id = db.latest_run_id()
    if not run_id:
        raise HTTPException(404, "No runs yet. Call POST /reconcile/run first.")
    return run_id


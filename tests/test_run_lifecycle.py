"""Run lifecycle: a run row must exist from batch start as RUNNING and end COMPLETED or FAILED,
so a crash or a controlled failure can never leave orphaned decisions attached to a run_id that
has no run record."""
import pytest

from app import constants as C
from app import db
from app import settings as llm_settings
from app import pipeline
from app.pipeline import run_reconciliation


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(llm_settings, "_settings", None)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    db.init_db()
    yield tmp_path
    monkeypatch.setattr(llm_settings, "_settings", None)


def _write_raw(raw):
    raw.mkdir(exist_ok=True)
    (raw / "payments.csv").write_text(
        "payment_id,order_id,amount,currency,method,customer_name,customer_email,created_at,status,description\n"
        "pay_1,order_abc123456,1000.00,INR,upi,Acme Traders,a@x.com,2026-01-01T10:00:00,captured,p\n"
    )
    (raw / "bank_settlements.csv").write_text(
        "bank_ref,utr,settlement_date,amount,narration,payer_name,reference_hint\n"
        "bnk_1,UTR1,2026-01-02,1000.00,NEFT/UTR1/ACME/order_abc123456,Acme Traders,order_abc123456\n"
    )
    (raw / "invoices.csv").write_text(
        "invoice_id,order_id,amount,customer_name,invoice_date,description,status\n"
        "INV-1,order_abc123456,1000.00,Acme Traders,2026-01-01,inv,paid\n"
    )


def _run_statuses():
    with db.get_conn() as conn:
        return {r["run_id"]: dict(r) for r in conn.execute("SELECT * FROM runs").fetchall()}


def test_successful_run_goes_running_then_completed(tmp_env):
    raw = tmp_env / "raw"
    _write_raw(raw)
    metrics = run_reconciliation(raw)
    assert metrics["status"] == C.RUN_COMPLETED
    run = _run_statuses()[metrics["run_id"]]
    assert run["status"] == C.RUN_COMPLETED
    assert run["finished_at"] is not None
    assert run["error"] is None
    assert run["total_payments"] == 1


def test_run_works_on_a_database_that_does_not_exist_yet(tmp_path, monkeypatch):
    """A fresh checkout must reconcile without the API ever having been started.

    The run row is written before ingestion, so schema creation cannot be left to the ingestion
    step: `python cli.py run` on a clean clone previously died with `no such table: runs`. This
    fixture deliberately does NOT call `db.init_db()`.
    """
    import config

    db_path = tmp_path / "nested" / "fresh.db"
    db_path.parent.mkdir()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(llm_settings, "_settings", None)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    assert not db_path.exists()

    raw = tmp_path / "raw"
    _write_raw(raw)
    metrics = run_reconciliation(raw)

    assert metrics["status"] == C.RUN_COMPLETED
    assert metrics["total_payments"] == 1
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"] == 1
    monkeypatch.setattr(llm_settings, "_settings", None)


def test_run_row_is_written_at_start_not_just_at_the_end(tmp_env, monkeypatch):
    """If the run row only appeared after a successful batch, a mid-run failure would leave
    decisions pointing at a run_id with no runs row. Verify the row is open (RUNNING) before any
    decision is persisted by failing the very first step that happens inside the run."""
    raw = tmp_env / "raw"
    _write_raw(raw)
    run_ids_seen = []

    real_open = pipeline._open_run

    def spy_open(conn, run_id, started_at, ai):
        real_open(conn, run_id, started_at, ai)
        run_ids_seen.append(run_id)

    # Fail inside _execute_run right after the run row is opened, before any decision is written.
    monkeypatch.setattr(pipeline, "_open_run", spy_open)
    monkeypatch.setattr(pipeline, "ingest_all", lambda raw_dir: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        run_reconciliation(raw)

    assert len(run_ids_seen) == 1
    run = _run_statuses()[run_ids_seen[0]]
    assert run["status"] == C.RUN_FAILED
    assert "boom" in run["error"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"] == 0


def test_mid_batch_failure_marks_the_run_failed_with_error(tmp_env, monkeypatch):
    """A failure part-way through processing must yield a FAILED run, not orphaned decisions."""
    raw = tmp_env / "raw"
    _write_raw(raw)

    real_generate = pipeline.generate_candidates
    calls = {"n": 0}

    def explode_after_first(payment, source, thresholds):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("kaboom mid-batch")
        return real_generate(payment, source, thresholds)

    monkeypatch.setattr(pipeline, "generate_candidates", explode_after_first)

    # Give it two payments so the failure lands after the first decision is committed.
    with open(raw / "payments.csv", "a") as f:
        f.write("pay_2,order_xyz987654,500.00,INR,upi,Beta Co,b@x.com,2026-01-01T10:00:00,captured,p\n")

    with pytest.raises(RuntimeError):
        run_reconciliation(raw)

    with db.get_conn() as conn:
        runs = {r["run_id"]: dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()}
        committed = conn.execute("SELECT run_id, COUNT(*) n FROM decisions GROUP BY run_id").fetchall()
    failed = runs[max(runs, key=lambda k: runs[k]["started_at"])]
    assert failed["status"] == C.RUN_FAILED
    assert "kaboom" in failed["error"]
    # every committed decision belongs to a run that HAS a run row (the FAILED one)
    for row in committed:
        assert row["run_id"] in runs
"""Tests for evaluation reproducibility: a run must be scored against the ground truth it
actually ran against, not whatever ground_truth.csv happens to be on disk when `evaluate()`
is later called (the raw dataset may have been regenerated/overwritten in between).
"""
import pytest

from app import db
from app.evaluation import evaluate


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield


def _insert_decision(conn, run_id, payment_id, status, matched_bank_ref):
    conn.execute(
        """INSERT INTO decisions
           (run_id, payment_id, status, category, matched_bank_ref, confidence, method, ai_used,
            reason, invoice_id, invoice_status, evidence_json, processing_ms, created_at)
           VALUES (?,?,?,NULL,?,100,'rule',0,'test','', '', '{}', 1.0, '2026-01-01T00:00:00')""",
        (run_id, payment_id, status, matched_bank_ref),
    )


def test_evaluate_uses_run_snapshot_not_current_raw_file(tmp_path, tmp_db):
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO run_ground_truth
               (run_id, payment_id, true_bank_ref, true_invoice_id, case_type, is_safely_resolvable, notes)
               VALUES ('run1', 'pay_1', 'bnk_A', '', 'exact_match', 1, '')"""
        )
        _insert_decision(conn, "run1", "pay_1", "AUTO_MATCH", "bnk_A")

    # The raw file on disk NOW disagrees with what run1 actually saw (as if the dataset had
    # since been regenerated). evaluate(run1) must still score against the snapshot.
    (tmp_path / "ground_truth.csv").write_text(
        "payment_id,true_bank_ref,true_invoice_id,case_type,is_safely_resolvable,notes\n"
        "pay_1,bnk_DIFFERENT,,exact_match,True,regenerated dataset\n"
    )

    result = evaluate("run1", tmp_path)
    assert result["ground_truth_source"] == "run_snapshot"
    assert result["outcomes"]["CORRECT_AUTO"] == 1
    assert result["outcomes"]["INCORRECT_AUTO"] == 0


def test_evaluate_falls_back_to_raw_file_when_no_snapshot_exists(tmp_path, tmp_db):
    with db.get_conn() as conn:
        _insert_decision(conn, "run2", "pay_2", "AUTO_MATCH", "bnk_B")

    (tmp_path / "ground_truth.csv").write_text(
        "payment_id,true_bank_ref,true_invoice_id,case_type,is_safely_resolvable,notes\n"
        "pay_2,bnk_B,,exact_match,True,no snapshot for this run\n"
    )

    result = evaluate("run2", tmp_path)
    assert result["ground_truth_source"] == "current_raw_file_fallback"
    assert result["outcomes"]["CORRECT_AUTO"] == 1

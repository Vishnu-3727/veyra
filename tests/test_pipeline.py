"""Tests for pipeline-level evidence handling that must never silently discard a record, and
for the batch's fail-closed behavior when AI is unavailable."""
import json

import pytest

from app import constants as C
from app import db
from app import settings as llm_settings
from app.pipeline import _corroborate_invoice, run_reconciliation
from config import THRESHOLDS


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(llm_settings, "_settings", None)
    monkeypatch.setattr(config, "LLM_API_KEY", "")  # AI disabled -> fail-closed path
    db.init_db()
    yield tmp_path
    monkeypatch.setattr(llm_settings, "_settings", None)


def _write_sources(raw_dir, payments, banks, invoices):
    raw_dir.mkdir(exist_ok=True)
    (raw_dir / "payments.csv").write_text(
        "payment_id,order_id,amount,currency,method,customer_name,customer_email,created_at,status,description\n"
        + "".join(payments)
    )
    (raw_dir / "bank_settlements.csv").write_text(
        "bank_ref,utr,settlement_date,amount,narration,payer_name,reference_hint\n" + "".join(banks)
    )
    (raw_dir / "invoices.csv").write_text(
        "invoice_id,order_id,amount,customer_name,invoice_date,description,status\n" + "".join(invoices)
    )


def test_single_invoice_for_order_is_corroborated_normally():
    payment = {"order_id": "order_1", "amount": 100.0}
    invoices_by_order = {"order_1": [{"invoice_id": "INV-1", "order_id": "order_1", "amount": 100.0}]}
    invoice, status = _corroborate_invoice(payment, invoices_by_order, THRESHOLDS)
    assert status == C.INVOICE_FOUND_CONSISTENT
    assert invoice["invoice_id"] == "INV-1"


def test_no_invoice_for_order_is_not_found():
    payment = {"order_id": "order_1", "amount": 100.0}
    invoice, status = _corroborate_invoice(payment, {}, THRESHOLDS)
    assert invoice is None
    assert status == C.INVOICE_NOT_FOUND


def test_multiple_invoices_sharing_order_id_are_flagged_ambiguous_not_silently_dropped():
    """Previously, building invoices_by_order as a plain {order_id: invoice} dict meant a
    second invoice for the same order_id silently overwrote (discarded) the first. Evidence
    must be surfaced as ambiguous, never silently discarded."""
    payment = {"order_id": "order_1", "amount": 100.0}
    invoices_by_order = {
        "order_1": [
            {"invoice_id": "INV-1", "order_id": "order_1", "amount": 100.0},
            {"invoice_id": "INV-2", "order_id": "order_1", "amount": 100.0},
        ],
    }
    invoice, status = _corroborate_invoice(payment, invoices_by_order, THRESHOLDS)
    assert status == C.INVOICE_AMBIGUOUS
    assert invoice is not None  # still surfaces one of them as a reference point, not silently None


def test_invoice_present_but_unreadable_is_distinct_from_no_invoice():
    payment = {"order_id": "order_1", "amount": 100.0}
    invoice, status = _corroborate_invoice(payment, {}, THRESHOLDS, {"order_1": ["INV-BAD"]})
    assert invoice is None
    assert status == C.INVOICE_RECORD_INVALID


def test_unusable_bank_record_does_not_match_but_is_reported_on_the_exception(tmp_env):
    """A malformed bank row must never become evidence, yet a reviewer chasing a "missing"
    settlement needs to know one existed and was unreadable."""
    raw = tmp_env / "raw"
    _write_sources(
        raw,
        payments=["pay_1,order_abc123456,1000.00,INR,upi,Acme Traders,a@x.com,2026-01-01T10:00:00,captured,p\n"],
        banks=["bnk_bad,UTR1,2026-01-02,NOT_A_NUMBER,NEFT/UTR1/ACME/order_abc123456,Acme Traders,order_abc123456\n"],
        invoices=["INV-BAD,order_abc123456,NOT_A_NUMBER,Acme Traders,2026-01-01,inv,paid\n"],
    )
    metrics = run_reconciliation(raw)
    assert metrics["status_counts"][C.STATUS_EXCEPTION] == 1
    assert metrics["ground_truth_snapshotted"] is False  # no ground_truth.csv in this dataset

    with db.get_conn() as conn:
        decision = dict(conn.execute("SELECT * FROM decisions WHERE payment_id = 'pay_1'").fetchone())
    assert decision["category"] == C.CAT_NO_CANDIDATE
    assert decision["matched_bank_ref"] is None
    assert decision["invoice_status"] == C.INVOICE_RECORD_INVALID
    evidence = json.loads(decision["evidence_json"])
    assert evidence["unusable_bank_records_matching_reference"] == ["bnk_bad"]
    assert "failed source validation" in evidence["why_unresolved"]


def test_run_snapshots_the_payments_it_processed(tmp_env):
    raw = tmp_env / "raw"
    _write_sources(
        raw,
        payments=["pay_1,order_abc123456,1000.00,INR,upi,Acme Traders,a@x.com,2026-01-01T10:00:00,captured,p\n"],
        banks=["bnk_1,UTR1,2026-01-02,1000.00,NEFT/UTR1/ACME/order_abc123456,Acme Traders,order_abc123456\n"],
        invoices=["INV-1,order_abc123456,1000.00,Acme Traders,2026-01-01,inv,paid\n"],
    )
    metrics = run_reconciliation(raw)
    assert metrics["status_counts"][C.STATUS_AUTO_MATCH] == 1

    with db.get_conn() as conn:
        snap = dict(conn.execute(
            "SELECT * FROM run_payments WHERE run_id = ? AND payment_id = 'pay_1'", (metrics["run_id"],),
        ).fetchone())
    assert snap["customer_name"] == "Acme Traders"
    assert snap["amount"] == 1000.0
    assert snap["validation_status"] == "ok"


def test_ambiguous_case_without_a_configured_provider_fails_closed_to_an_exception(tmp_env):
    """No API key must never mean "guess": the ambiguous case becomes an explicit
    AI_UNAVAILABLE exception, and the batch still completes."""
    raw = tmp_env / "raw"
    _write_sources(
        raw,
        payments=["pay_1,order_abc123456,1000.00,INR,upi,Acme Traders,a@x.com,2026-01-01T10:00:00,captured,p\n"],
        # exact amount, plausible-but-not-high name similarity, only a partial reference trace
        banks=["bnk_1,UTR9,2026-01-02,1000.00,UPI/123456/ACME TRD,Acme Trd,\n"],
        invoices=["INV-1,order_abc123456,1000.00,Acme Traders,2026-01-01,inv,paid\n"],
    )
    metrics = run_reconciliation(raw)
    assert metrics["ai_invocations"] == 1
    assert metrics["ai_enabled"] is False
    assert metrics["status_counts"][C.STATUS_EXCEPTION] == 1

    with db.get_conn() as conn:
        decision = dict(conn.execute("SELECT * FROM decisions WHERE payment_id = 'pay_1'").fetchone())
        exceptions = conn.execute("SELECT COUNT(*) c FROM exceptions WHERE run_id = ?", (metrics["run_id"],)).fetchone()["c"]
    assert decision["category"] == C.CAT_AI_UNAVAILABLE
    assert decision["matched_bank_ref"] is None
    assert exceptions == 1

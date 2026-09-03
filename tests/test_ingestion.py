"""Tests for graceful handling of malformed/missing input at the ingestion boundary."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app import db
from app.ingestion import ingest_all


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    yield


def test_missing_source_file_raises_filenotfound(tmp_path, tmp_db):
    with pytest.raises(FileNotFoundError):
        ingest_all(tmp_path)  # empty dir, no CSVs at all


def test_malformed_rows_are_flagged_invalid_not_silently_accepted(tmp_path, tmp_db):
    (tmp_path / "payments.csv").write_text(
        "payment_id,order_id,amount,currency,method,customer_name,customer_email,created_at,status,description\n"
        "pay_ok,order_1,100.00,INR,upi,Alice,alice@x.com,2026-08-01T10:00:00,captured,ok\n"
        "pay_bad_amount,order_2,NOT_A_NUMBER,INR,upi,Bob,bob@x.com,2026-08-01T10:00:00,captured,bad\n"
        "pay_no_date,order_3,50.00,INR,upi,Carl,carl@x.com,,captured,bad\n"
    )
    (tmp_path / "bank_settlements.csv").write_text(
        "bank_ref,utr,settlement_date,amount,narration,payer_name,reference_hint\n"
        "bnk_ok,UTR1,2026-08-02,100.00,NEFT/UTR1/ALICE/order1,Alice,order1\n"
    )
    (tmp_path / "invoices.csv").write_text(
        "invoice_id,order_id,amount,customer_name,invoice_date,description,status\n"
        "INV-1,order_1,100.00,Alice,2026-08-01,inv,paid\n"
    )

    reports = ingest_all(tmp_path)
    assert reports["payments"].rows_read == 3
    assert reports["payments"].rows_valid == 1
    assert reports["payments"].rows_invalid == 2

    with db.get_conn() as conn:
        rows = {r["payment_id"]: dict(r) for r in conn.execute("SELECT * FROM payments").fetchall()}
    assert rows["pay_ok"]["validation_status"] == "ok"
    assert rows["pay_bad_amount"]["validation_status"] == "invalid"
    assert rows["pay_bad_amount"]["amount"] is None  # never silently coerced to a guessed number
    assert rows["pay_no_date"]["validation_status"] == "invalid"


def test_empty_bank_file_does_not_crash_ingestion(tmp_path, tmp_db):
    (tmp_path / "payments.csv").write_text(
        "payment_id,order_id,amount,currency,method,customer_name,customer_email,created_at,status,description\n"
        "pay_ok,order_1,100.00,INR,upi,Alice,alice@x.com,2026-08-01T10:00:00,captured,ok\n"
    )
    (tmp_path / "bank_settlements.csv").write_text(
        "bank_ref,utr,settlement_date,amount,narration,payer_name,reference_hint\n"
    )
    (tmp_path / "invoices.csv").write_text(
        "invoice_id,order_id,amount,customer_name,invoice_date,description,status\n"
    )
    reports = ingest_all(tmp_path)
    assert reports["bank_settlements"].rows_read == 0
    assert reports["invoices"].rows_read == 0

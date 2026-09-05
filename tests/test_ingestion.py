"""Tests for graceful handling of malformed/missing input at the ingestion boundary."""
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


def test_row_without_a_primary_key_is_dropped_not_guessed(tmp_path, tmp_db):
    (tmp_path / "payments.csv").write_text(
        "payment_id,order_id,amount,currency,method,customer_name,customer_email,created_at,status,description\n"
        "pay_ok,order_1,100.00,INR,upi,Alice,alice@x.com,2026-08-01T10:00:00,captured,ok\n"
        ",order_2,100.00,INR,upi,Bob,bob@x.com,2026-08-01T10:00:00,captured,no id\n"
    )
    (tmp_path / "bank_settlements.csv").write_text(
        "bank_ref,utr,settlement_date,amount,narration,payer_name,reference_hint\n"
        ",UTR1,2026-08-02,100.00,NEFT/UTR1,Alice,order1\n"
    )
    (tmp_path / "invoices.csv").write_text(
        "invoice_id,order_id,amount,customer_name,invoice_date,description,status\n"
    )

    reports = ingest_all(tmp_path)
    assert reports["payments"].rows_invalid == 1
    assert reports["payments"].errors == ["row skipped: missing payment_id"]
    assert reports["bank_settlements"].errors == ["row skipped: missing bank_ref"]

    with db.get_conn() as conn:
        assert [r["payment_id"] for r in conn.execute("SELECT payment_id FROM payments")] == ["pay_ok"]
        assert conn.execute("SELECT COUNT(*) c FROM bank_settlements").fetchone()["c"] == 0


def test_duplicate_primary_key_is_flagged_not_silently_overwritten(tmp_path, tmp_db):
    """A repeated payment_id must never silently replace the first record ingested under that
    key (INSERT OR REPLACE previously made one of two real financial records vanish)."""
    (tmp_path / "payments.csv").write_text(
        "payment_id,order_id,amount,currency,method,customer_name,customer_email,created_at,status,description\n"
        "pay_1,order_1,100.00,INR,upi,Alice,alice@x.com,2026-08-01T10:00:00,captured,first\n"
        "pay_1,order_9,999.00,INR,upi,Zed,zed@x.com,2026-08-05T10:00:00,captured,second\n"
    )
    (tmp_path / "bank_settlements.csv").write_text(
        "bank_ref,utr,settlement_date,amount,narration,payer_name,reference_hint\n"
        "bnk_ok,UTR1,2026-08-02,100.00,NEFT/UTR1/ALICE/order1,Alice,order1\n"
        "bnk_ok,UTR2,2026-08-03,50.00,NEFT/UTR2/DUP,Zed,order9\n"
    )
    (tmp_path / "invoices.csv").write_text(
        "invoice_id,order_id,amount,customer_name,invoice_date,description,status\n"
    )

    reports = ingest_all(tmp_path)
    assert reports["payments"].rows_read == 2
    assert reports["payments"].rows_valid == 1
    assert reports["payments"].rows_invalid == 1
    assert reports["payments"].errors == ["row skipped: duplicate payment_id=pay_1"]
    assert reports["bank_settlements"].errors == ["row skipped: duplicate bank_ref=bnk_ok"]

    with db.get_conn() as conn:
        rows = {r["payment_id"]: dict(r) for r in conn.execute("SELECT * FROM payments").fetchall()}
        bank_rows = {r["bank_ref"]: dict(r) for r in conn.execute("SELECT * FROM bank_settlements").fetchall()}
    assert list(rows) == ["pay_1"]
    # the FIRST record under the duplicated key survives untouched -- not overwritten by the second
    assert rows["pay_1"]["order_id"] == "order_1"
    assert rows["pay_1"]["customer_name"] == "Alice"
    assert bank_rows["bnk_ok"]["utr"] == "UTR1"


def test_malformed_bank_and_invoice_rows_are_preserved_as_invalid(tmp_path, tmp_db):
    """A record that cannot be read is still a fact: keeping it flagged lets the engine tell
    "no bank/invoice record exists" apart from "a record exists but was unusable"."""
    (tmp_path / "payments.csv").write_text(
        "payment_id,order_id,amount,currency,method,customer_name,customer_email,created_at,status,description\n"
        "pay_ok,order_1,100.00,INR,upi,Alice,alice@x.com,2026-08-01T10:00:00,captured,ok\n"
    )
    (tmp_path / "bank_settlements.csv").write_text(
        "bank_ref,utr,settlement_date,amount,narration,payer_name,reference_hint\n"
        "bnk_ok,UTR1,2026-08-02,100.00,NEFT/UTR1/ALICE/order1,Alice,order_1\n"
        "bnk_bad_amount,UTR2,2026-08-02,NOT_A_NUMBER,NEFT/UTR2/ALICE/order1,Alice,order_1\n"
        "bnk_bad_date,UTR3,,100.00,NEFT/UTR3/ALICE/order1,Alice,order_1\n"
    )
    (tmp_path / "invoices.csv").write_text(
        "invoice_id,order_id,amount,customer_name,invoice_date,description,status\n"
        "INV-1,order_1,100.00,Alice,2026-08-01,inv,paid\n"
        "INV-BAD,order_2,NOT_A_NUMBER,Bob,2026-08-01,inv,paid\n"
    )

    reports = ingest_all(tmp_path)
    assert reports["bank_settlements"].rows_read == 3
    assert reports["bank_settlements"].rows_valid == 1
    assert reports["bank_settlements"].rows_invalid == 2
    assert reports["invoices"].rows_invalid == 1

    with db.get_conn() as conn:
        bank_rows = {r["bank_ref"]: dict(r) for r in conn.execute("SELECT * FROM bank_settlements")}
        invoices = {r["invoice_id"]: dict(r) for r in conn.execute("SELECT * FROM invoices")}
    assert bank_rows["bnk_ok"]["validation_status"] == "ok"
    assert bank_rows["bnk_bad_amount"]["validation_status"] == "invalid"
    assert bank_rows["bnk_bad_amount"]["amount"] is None  # never guessed
    assert bank_rows["bnk_bad_date"]["validation_status"] == "invalid"
    assert invoices["INV-BAD"]["validation_status"] == "invalid"
    assert invoices["INV-1"]["validation_status"] == "ok"


def test_non_finite_amounts_are_flagged_invalid_at_ingestion(tmp_path, tmp_db):
    """CASE 4: `inf` is not a monetary amount. It must fail validation, not enter the ledger."""
    (tmp_path / "payments.csv").write_text(
        "payment_id,order_id,amount,currency,method,customer_name,customer_email,created_at,status,description\n"
        "pay_inf,order_1,inf,INR,upi,Alice,alice@x.com,2026-08-01T10:00:00,captured,inf\n"
        "pay_neg_inf,order_2,-Infinity,INR,upi,Bob,bob@x.com,2026-08-01T10:00:00,captured,neg inf\n"
        "pay_ok,order_3,100.00,INR,upi,Carl,carl@x.com,2026-08-01T10:00:00,captured,ok\n"
    )
    (tmp_path / "bank_settlements.csv").write_text(
        "bank_ref,utr,settlement_date,amount,narration,payer_name,reference_hint\n"
    )
    (tmp_path / "invoices.csv").write_text(
        "invoice_id,order_id,amount,customer_name,invoice_date,description,status\n"
    )

    reports = ingest_all(tmp_path)
    assert reports["payments"].rows_valid == 1
    assert reports["payments"].rows_invalid == 2

    with db.get_conn() as conn:
        rows = {r["payment_id"]: dict(r) for r in conn.execute("SELECT * FROM payments")}
    for pid in ("pay_inf", "pay_neg_inf"):
        assert rows[pid]["validation_status"] == "invalid"
        assert rows[pid]["amount"] is None
        assert "amount" in rows[pid]["validation_error"]
    assert rows["pay_ok"]["validation_status"] == "ok"

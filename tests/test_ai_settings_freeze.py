"""AI configuration must be frozen per run: the whole batch uses one provider/model/key snapshot
taken at run start, so an operator changing settings mid-batch (or a test mutating live settings
between payments) can never split one run across two providers."""
import pytest

from app import ai_reasoning
from app import db
from app import settings as llm_settings
from app import pipeline
from app.ai_reasoning import AIResult
from app.pipeline import run_reconciliation


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key-1234")
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "nvidia/nemotron-nano-9b-v2:free")
    monkeypatch.setattr(llm_settings, "_settings", None)
    db.init_db()
    yield tmp_path
    monkeypatch.setattr(llm_settings, "_settings", None)


def _write_ambiguous_raw(raw):
    raw.mkdir(exist_ok=True)
    # Two payments whose only evidence is an exact amount plus a weak (but not high) name match,
    # with no clean reference trace -- both force the NEEDS_AI path so reason_about_candidates is
    # invoked twice, letting us assert both calls received the same frozen settings snapshot.
    payments = (
        "payment_id,order_id,amount,currency,method,customer_name,customer_email,created_at,status,description\n"
        "pay_1,order_abc123456,1000.00,INR,upi,Acme Traders,a@x.com,2026-01-01T10:00:00,captured,p\n"
        "pay_2,order_def654321,2000.00,INR,upi,Beta Enterprises,b@x.com,2026-01-01T10:00:00,captured,p\n"
    )
    banks = (
        "bank_ref,utr,settlement_date,amount,narration,payer_name,reference_hint\n"
        "bnk_1,UTR1,2026-01-02,1000.00,PAYMENT UDP,Acme Trd,\n"
        "bnk_2,UTR2,2026-01-02,2000.00,PAYMENT UDP,Beta Ent,\n"
    )
    invoices = (
        "invoice_id,order_id,amount,customer_name,invoice_date,description,status\n"
        "INV-1,order_abc123456,1000.00,Acme Traders,2026-01-01,inv,paid\n"
        "INV-2,order_def654321,2000.00,Beta Enterprises,2026-01-01,inv,paid\n"
    )
    (raw / "payments.csv").write_text(payments)
    (raw / "bank_settlements.csv").write_text(banks)
    (raw / "invoices.csv").write_text(invoices)


def _make_no_match(*_args, **_kwargs):
    return AIResult(decision="NO_MATCH", confidence=20, reasoning="synthetic: declined")


def test_all_ai_calls_in_one_run_use_the_frozen_configuration(tmp_env, monkeypatch):
    raw = tmp_env / "raw"
    _write_ambiguous_raw(raw)

    captured_settings = []

    def spy_reason(payment, candidates, settings=None):
        captured_settings.append(settings)
        # Simulate an operator changing the LIVE provider after the run has already begun: this
        # must not affect the run already in flight.
        if len(captured_settings) == 1:
            llm_settings.update(provider="groq", api_key="groq-different-key")
        return _make_no_match()

    monkeypatch.setattr(pipeline, "reason_about_candidates", spy_reason)

    metrics = run_reconciliation(raw)

    assert len(captured_settings) >= 2, "expected the ambiguous dataset to trigger at least two AI calls"
    # Every call got the SAME frozen snapshot object, not a fresh live read.
    assert all(s is captured_settings[0] for s in captured_settings)
    # The frozen config reflects run start (openrouter), not the mid-run groq change.
    assert captured_settings[0].provider == "openrouter"
    assert captured_settings[0].api_key == "test-key-1234"
    # and it matches what the run recorded as its AI configuration (which never includes the key)
    assert metrics["ai_config"]["provider"] == "openrouter"
    assert metrics["ai_config"]["model"] == "nvidia/nemotron-nano-9b-v2:free"
    assert "test-key-1234" not in str(metrics["ai_config"])
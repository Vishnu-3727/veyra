"""Tests for the deterministic decision tree -- specifically the settlement-date
guardrail on auto-matching. An exact reference and exact amount are strong evidence,
but if the settlement date falls outside the expected window, that is itself a
conflicting signal and must not be silently overridden by the other two.
"""
from app import constants as C
from app.candidates import Candidate
from app.scoring import NEEDS_AI, decide_deterministic
from config import THRESHOLDS


def make_candidate(date_diff_days: int, ref_match: str = "EXACT", amount_diff_pct: float = 0.0,
                    name_sim: int = 95, bank_ref: str = "bnk_1") -> Candidate:
    return Candidate(
        bank_ref=bank_ref, utr="UTR1", settlement_date="2026-08-01", amount=1000.0,
        narration="test", payer_name="Test Co", reference_hint="",
        amount_diff_abs=1000.0 * amount_diff_pct, amount_diff_pct=amount_diff_pct,
        date_diff_days=date_diff_days, ref_match=ref_match, name_sim=name_sim,
    )


def test_exact_reference_and_amount_within_window_is_auto_matched():
    c = make_candidate(date_diff_days=3)  # inside the default 7-day settlement window
    outcome = decide_deterministic([c], THRESHOLDS)
    assert outcome.status == C.STATUS_AUTO_MATCH
    assert outcome.matched.bank_ref == "bnk_1"


def test_exact_reference_and_amount_outside_settlement_window_is_not_auto_matched():
    """This is the core guardrail: exact reference + exact amount must NOT bypass the
    settlement-date window. A 31-day-late settlement with an otherwise-clean exact match
    is conflicting evidence (a coincidentally-reused/duplicate reference is exactly this
    shape), not a safe automatic match."""
    c = make_candidate(date_diff_days=31)  # far outside the default 7-day window
    outcome = decide_deterministic([c], THRESHOLDS)
    assert outcome.status != C.STATUS_AUTO_MATCH
    assert outcome.status == NEEDS_AI


def test_exact_amount_high_name_similarity_outside_window_is_not_auto_matched():
    c = make_candidate(date_diff_days=45, ref_match="PARTIAL", name_sim=95)
    outcome = decide_deterministic([c], THRESHOLDS)
    assert outcome.status != C.STATUS_AUTO_MATCH


def test_dominant_candidate_among_multiple_outside_window_is_not_auto_matched():
    """Same guardrail, but through the multi-candidate "dominant match" branch."""
    dominant = make_candidate(date_diff_days=40, bank_ref="bnk_1")
    weaker = make_candidate(date_diff_days=2, ref_match="PARTIAL", amount_diff_pct=0.03,
                             name_sim=60, bank_ref="bnk_2")
    outcome = decide_deterministic([dominant, weaker], THRESHOLDS)
    assert outcome.status != C.STATUS_AUTO_MATCH

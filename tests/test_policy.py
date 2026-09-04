"""Tests for the policy guardrail layer -- the boundary that stops an
unsupported AI response from becoming a financial decision.
"""
from app import constants as C
from app.ai_reasoning import AIResult
from app.candidates import Candidate
from app.policy import apply_ai_policy
from config import THRESHOLDS


def make_candidate(bank_ref="bnk_1", amount_diff_pct=0.0, name_sim=90) -> Candidate:
    return Candidate(
        bank_ref=bank_ref, utr="UTR1", settlement_date="2026-08-01", amount=1000.0,
        narration="test", payer_name="Test Co", reference_hint="",
        amount_diff_abs=1000.0 * amount_diff_pct, amount_diff_pct=amount_diff_pct,
        date_diff_days=1, ref_match="PARTIAL", name_sim=name_sim,
    )


def test_ai_error_routes_to_ai_unavailable():
    outcome = apply_ai_policy(AIResult(decision="ERROR", error="timeout"), [make_candidate()], THRESHOLDS)
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.category == C.CAT_AI_UNAVAILABLE
    assert outcome.matched is None


def test_ai_no_match_routes_to_low_confidence():
    outcome = apply_ai_policy(AIResult(decision="NO_MATCH", confidence=20), [make_candidate()], THRESHOLDS)
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.category == C.CAT_LOW_CONFIDENCE


def test_ai_match_with_valid_candidate_and_high_confidence_is_approved():
    cand = make_candidate(bank_ref="bnk_1", amount_diff_pct=0.01)
    ai = AIResult(decision="MATCH", candidate_id="bnk_1", confidence=90, reasoning="clear match")
    outcome = apply_ai_policy(ai, [cand], THRESHOLDS)
    assert outcome.status == C.STATUS_AI_ASSISTED_MATCH
    assert outcome.matched.bank_ref == "bnk_1"


def test_ai_match_referencing_unknown_candidate_is_rejected():
    """An AI cannot conjure a candidate outside the evaluated set."""
    ai = AIResult(decision="MATCH", candidate_id="bnk_does_not_exist", confidence=95, reasoning="hallucinated")
    outcome = apply_ai_policy(ai, [make_candidate(bank_ref="bnk_1")], THRESHOLDS)
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.category == C.CAT_UNSUPPORTED_AI


def test_ai_match_below_confidence_threshold_is_rejected():
    cand = make_candidate(bank_ref="bnk_1", amount_diff_pct=0.0)
    ai = AIResult(decision="MATCH", candidate_id="bnk_1", confidence=50, reasoning="not very sure")
    outcome = apply_ai_policy(ai, [cand], THRESHOLDS)
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.category == C.CAT_LOW_CONFIDENCE


def test_ai_match_exceeding_hard_amount_cap_is_overridden_regardless_of_confidence():
    """This is the core product guarantee: AI confidence can NEVER override
    the hard amount-mismatch cap."""
    cand = make_candidate(bank_ref="bnk_1", amount_diff_pct=0.20)  # 20% > 8% hard cap
    ai = AIResult(decision="MATCH", candidate_id="bnk_1", confidence=99, reasoning="very confident but wrong")
    outcome = apply_ai_policy(ai, [cand], THRESHOLDS)
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.category == C.CAT_UNSUPPORTED_AI
    assert outcome.matched is None

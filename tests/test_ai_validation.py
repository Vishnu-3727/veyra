"""Tests for AI response schema validation and its handoff to policy.

A model response is untrusted input: these tests pin the guarantee that no
shape of malformed LLM output can raise out of validation or out of the
policy layer (the original bug was an unhashable candidate_id crashing the
dict lookup in app/policy.py with TypeError), and that structurally invalid
content always fails closed into an explicit exception.
"""
import pytest

from app import constants as C
from app.ai_reasoning import AIResult, validate_ai_payload
from app.candidates import Candidate
from app.policy import apply_ai_policy
from config import THRESHOLDS


def make_candidate(bank_ref="bnk_1", amount_diff_pct=0.0, name_sim=90, date_diff_days=1) -> Candidate:
    return Candidate(
        bank_ref=bank_ref, utr="UTR1", settlement_date="2026-08-01", amount=1000.0,
        narration="test", payer_name="Test Co", reference_hint="",
        amount_diff_abs=1000.0 * amount_diff_pct, amount_diff_pct=amount_diff_pct,
        date_diff_days=date_diff_days, ref_match="PARTIAL", name_sim=name_sim,
    )


def validate_and_apply(parsed):
    """Run the full untrusted path: raw parsed payload -> validation -> policy."""
    result = validate_ai_payload(parsed, model="test-model", latency_ms=12.5)
    return result, apply_ai_policy(result, [make_candidate()], THRESHOLDS)


def assert_rejected(parsed):
    result, outcome = validate_and_apply(parsed)
    assert result.decision == "INVALID", result
    assert result.error and result.error.startswith("AI response violated the required output schema:")
    assert result.model == "test-model" and result.latency_ms == 12.5
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.category == C.CAT_UNSUPPORTED_AI
    assert outcome.matched is None
    return result, outcome


def test_unhashable_candidate_id_from_audit_does_not_crash_the_pipeline():
    """The exact adversarial payload from the audit: `candidate_id: []` used to
    raise TypeError: unhashable type inside the policy dict lookup."""
    result, outcome = validate_and_apply({"decision": "MATCH", "candidate_id": [], "confidence": 99})
    assert result.decision == "INVALID"
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.matched is None


@pytest.mark.parametrize("candidate_id", [[], {}, ["bnk_1"], {"id": "bnk_1"}, 7, 7.5, True])
def test_non_string_candidate_id_is_rejected(candidate_id):
    assert_rejected({"decision": "MATCH", "candidate_id": candidate_id, "confidence": 99})


@pytest.mark.parametrize("payload", [
    {"decision": "MATCH", "candidate_id": None, "confidence": 99},
    {"decision": "MATCH", "confidence": 99},
    {"decision": "MATCH", "candidate_id": "", "confidence": 99},
    {"decision": "MATCH", "candidate_id": "   ", "confidence": 99},
])
def test_match_without_a_named_candidate_is_rejected(payload):
    """A MATCH that names no candidate cannot be checked against the evaluated
    candidate set, so it can never be honored."""
    assert_rejected(payload)


@pytest.mark.parametrize("confidence", ["high", {}, [], None, 150, -5, 101, True, False, 90.5, "9o"])
def test_non_integral_or_out_of_range_confidence_is_rejected(confidence):
    """Out-of-range scores are rejected outright rather than clamped, so a
    nonsense score can never be rounded into an approvable one."""
    assert_rejected({"decision": "MATCH", "candidate_id": "bnk_1", "confidence": confidence})


def test_missing_confidence_is_rejected():
    assert_rejected({"decision": "MATCH", "candidate_id": "bnk_1"})


@pytest.mark.parametrize("risk_flags", ["bad", [1, 2], ["ok", 3], [True], {"flag": "bad"}, 5])
def test_malformed_risk_flags_are_rejected(risk_flags):
    """A bare string would silently iterate into a list of characters."""
    assert_rejected({
        "decision": "MATCH", "candidate_id": "bnk_1", "confidence": 90, "risk_flags": risk_flags,
    })


@pytest.mark.parametrize("decision", ["MAYBE", "", "match ish", None, 1, ["MATCH"], {"decision": "MATCH"}, True])
def test_unrecognized_or_non_string_decision_is_rejected(decision):
    assert_rejected({"decision": decision, "candidate_id": "bnk_1", "confidence": 90})


def test_missing_decision_is_rejected():
    assert_rejected({"candidate_id": "bnk_1", "confidence": 90})


@pytest.mark.parametrize("parsed", [
    [{"decision": "MATCH"}],
    '{"decision": "MATCH"}',
    None,
    42,
    ("decision", "MATCH"),
])
def test_payload_that_is_not_an_object_is_rejected(parsed):
    assert_rejected(parsed)


@pytest.mark.parametrize("reasoning", [1, {}, ["because"]])
def test_non_string_reasoning_is_rejected(reasoning):
    assert_rejected({
        "decision": "MATCH", "candidate_id": "bnk_1", "confidence": 90, "reasoning": reasoning,
    })


def test_valid_match_payload_validates_and_is_approved_by_policy():
    parsed = {
        "decision": "match", "candidate_id": "bnk_1", "confidence": 90,
        "reasoning": "reference and amount agree", "risk_flags": ["name_partial"],
    }
    result = validate_ai_payload(parsed, model="test-model", latency_ms=20.0)
    assert result.decision == "MATCH"
    assert result.candidate_id == "bnk_1"
    assert result.confidence == 90 and isinstance(result.confidence, int)
    assert result.reasoning == "reference and amount agree"
    assert result.risk_flags == ["name_partial"]
    assert result.error is None

    outcome = apply_ai_policy(result, [make_candidate(amount_diff_pct=0.01, date_diff_days=2)], THRESHOLDS)
    assert outcome.status == C.STATUS_AI_ASSISTED_MATCH
    assert outcome.matched.bank_ref == "bnk_1"


def test_valid_no_match_payload_validates_and_routes_to_low_confidence():
    result = validate_ai_payload({"decision": "NO_MATCH", "candidate_id": None, "confidence": 20,
                                  "reasoning": "evidence conflicts"})
    assert result.decision == "NO_MATCH"
    assert result.error is None

    outcome = apply_ai_policy(result, [make_candidate()], THRESHOLDS)
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.category == C.CAT_LOW_CONFIDENCE
    assert outcome.matched is None


def test_no_match_tolerates_a_stray_candidate_id_without_matching_it():
    result = validate_ai_payload({"decision": "NO_MATCH", "candidate_id": "bnk_1", "confidence": 95})
    assert result.decision == "NO_MATCH"
    outcome = apply_ai_policy(result, [make_candidate()], THRESHOLDS)
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.matched is None


@pytest.mark.parametrize("confidence", [90, 90.0, "90", " 90 ", 0, 100])
def test_integral_confidence_forms_coerce_to_int(confidence):
    result = validate_ai_payload({"decision": "NO_MATCH", "confidence": confidence})
    assert result.decision == "NO_MATCH"
    assert isinstance(result.confidence, int) and not isinstance(result.confidence, bool)
    assert result.confidence == int(float(str(confidence).strip()))


def test_optional_fields_default_when_absent():
    result = validate_ai_payload({"decision": "MATCH", "candidate_id": "bnk_1", "confidence": 80})
    assert result.reasoning == ""
    assert result.risk_flags == []


def test_policy_rejects_unhashable_candidate_id_even_without_validation():
    """Belt and braces: the policy layer must be safe on its own terms if a
    future caller ever skips validation."""
    outcome = apply_ai_policy(
        AIResult(decision="MATCH", candidate_id=[], confidence=99), [make_candidate()], THRESHOLDS)
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.category == C.CAT_UNSUPPORTED_AI
    assert outcome.matched is None


def test_policy_fails_closed_on_an_unknown_decision_value():
    outcome = apply_ai_policy(
        AIResult(decision="PROBABLY", candidate_id="bnk_1", confidence=99), [make_candidate()], THRESHOLDS)
    assert outcome.status == C.STATUS_EXCEPTION
    assert outcome.category == C.CAT_UNSUPPORTED_AI
    assert outcome.matched is None


def test_invalid_decision_routes_to_unsupported_ai_not_ai_unavailable():
    """An outage (ERROR) and unusable content (INVALID) are different failures
    and must stay distinguishable in the exception category."""
    invalid = apply_ai_policy(
        AIResult(decision="INVALID", error="schema violation"), [make_candidate()], THRESHOLDS)
    unavailable = apply_ai_policy(
        AIResult(decision="ERROR", error="timeout"), [make_candidate()], THRESHOLDS)
    assert invalid.category == C.CAT_UNSUPPORTED_AI
    assert "schema violation" in invalid.reason
    assert unavailable.category == C.CAT_AI_UNAVAILABLE

"""Decision/verification layer: enforces hard guardrails on AI output.

This is the boundary the product principle depends on -- an AI response can
recommend a match, but it can NEVER, by itself, create a reconciliation.
Every AI proposal is re-checked here against deterministic, non-negotiable
caps before it is allowed to become an AI_ASSISTED_MATCH. Anything that
fails is downgraded to an explicit exception, never silently approved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app import constants as C
from app.ai_reasoning import AIResult
from app.candidates import Candidate
from config import Thresholds


@dataclass
class PolicyOutcome:
    status: str
    category: Optional[str]
    matched: Optional[Candidate]
    reason: str


def apply_ai_policy(ai_result: AIResult, candidates: list[Candidate], thresholds: Thresholds) -> PolicyOutcome:
    by_id = {c.bank_ref: c for c in candidates}

    if ai_result.decision == "ERROR":
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_AI_UNAVAILABLE, matched=None,
            reason=f"AI reasoning could not be completed ({ai_result.error}); routed to human review "
                   f"rather than guessing.",
        )

    if ai_result.decision == "INVALID":
        # The provider answered, but the response did not satisfy the output
        # schema (see app.ai_reasoning.validate_ai_payload). Unusable content
        # is a rejected AI proposal, not an outage.
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_UNSUPPORTED_AI, matched=None,
            reason=f"AI response was structurally invalid ({ai_result.error}) and was rejected by policy "
                   f"guardrail; routed to human review.",
        )

    if ai_result.decision == "NO_MATCH":
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_LOW_CONFIDENCE, matched=None,
            reason=ai_result.reasoning or "AI found insufficient evidence to safely match any candidate.",
        )

    if ai_result.decision != "MATCH":
        # Fail closed on anything outside the known vocabulary: an unknown
        # decision value must never fall through to an approval.
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_UNSUPPORTED_AI, matched=None,
            reason=f"AI returned an unrecognized decision value {ai_result.decision!r}; "
                   f"rejected by policy guardrail.",
        )

    # decision == "MATCH" -- verify every guardrail before trusting it.
    # The candidate_id type is re-checked here even though validation should
    # already have rejected it: an unhashable value (e.g. [] or {}) would
    # otherwise raise TypeError inside the dict lookup below and take down
    # reconciliation, so this layer stays safe on its own terms.
    if not isinstance(ai_result.candidate_id, str) or not ai_result.candidate_id:
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_UNSUPPORTED_AI, matched=None,
            reason=f"AI proposed a match with an unusable candidate_id={ai_result.candidate_id!r} "
                   f"(expected a non-empty string) -- rejected by policy guardrail.",
        )

    candidate = by_id.get(ai_result.candidate_id)
    if candidate is None:
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_UNSUPPORTED_AI, matched=None,
            reason=f"AI proposed candidate_id={ai_result.candidate_id!r} which is not in the evaluated "
                   f"candidate set -- rejected by policy guardrail.",
        )

    if ai_result.confidence < thresholds.ai_confidence_threshold:
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_LOW_CONFIDENCE, matched=None,
            reason=f"AI confidence {ai_result.confidence} is below the required threshold "
                   f"({thresholds.ai_confidence_threshold}); a false match is more costly than a delay.",
        )

    if candidate.date_diff_days < 0 or candidate.date_diff_days > thresholds.settlement_window_days:
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_UNSUPPORTED_AI, matched=None,
            reason=f"AI approved a match with a settlement date {candidate.date_diff_days} day(s) from the "
                   f"payment date, outside the {thresholds.settlement_window_days}-day settlement window -- "
                   f"overridden by policy guardrail regardless of AI confidence.",
        )

    if candidate.amount_diff_pct > thresholds.ai_hard_amount_mismatch_cap_pct:
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_UNSUPPORTED_AI, matched=None,
            reason=f"AI approved a match with amount difference {candidate.amount_diff_pct:.1%}, exceeding the "
                   f"hard policy cap of {thresholds.ai_hard_amount_mismatch_cap_pct:.1%} -- overridden by "
                   f"policy guardrail regardless of AI confidence.",
        )

    # Deterministic evidence floor, re-asserted here rather than trusted from the escalation
    # step. Confidence/date/amount/membership were already re-checked above, but WHICH of the
    # escalated candidates gets approved was taken purely on the model's word -- and the model
    # reads attacker-influenceable free text (bank narration, payment description). A candidate
    # with no reference trace AND a weak name is not something the deterministic layer would
    # ever consider a contender (see app.scoring._is_plausible), so it must not become an
    # AI-assisted match because the prose around it was persuasive.
    if candidate.ref_match == "NONE" and candidate.name_sim < thresholds.min_name_similarity_for_ai:
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_UNSUPPORTED_AI, matched=None,
            reason=f"AI approved a candidate with no reference trace and name similarity "
                   f"{candidate.name_sim}/100 (below the {thresholds.min_name_similarity_for_ai} "
                   f"floor) -- overridden by policy guardrail regardless of AI confidence.",
        )

    return PolicyOutcome(
        status=C.STATUS_AI_ASSISTED_MATCH, category=None, matched=candidate,
        reason=ai_result.reasoning or "AI-assisted match approved within policy guardrails.",
    )

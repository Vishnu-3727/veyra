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

    if ai_result.decision == "NO_MATCH":
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_LOW_CONFIDENCE, matched=None,
            reason=ai_result.reasoning or "AI found insufficient evidence to safely match any candidate.",
        )

    # decision == "MATCH" -- verify every guardrail before trusting it
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

    if candidate.amount_diff_pct > thresholds.ai_hard_amount_mismatch_cap_pct:
        return PolicyOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_UNSUPPORTED_AI, matched=None,
            reason=f"AI approved a match with amount difference {candidate.amount_diff_pct:.1%}, exceeding the "
                   f"hard policy cap of {thresholds.ai_hard_amount_mismatch_cap_pct:.1%} -- overridden by "
                   f"policy guardrail regardless of AI confidence.",
        )

    return PolicyOutcome(
        status=C.STATUS_AI_ASSISTED_MATCH, category=None, matched=candidate,
        reason=ai_result.reasoning or "AI-assisted match approved within policy guardrails.",
    )

"""Exception management: structures every unresolved case so a human
reviewer can understand, without re-deriving anything, what the system
attempted, what evidence it found, what conflicted, why it refused to
resolve automatically, and what to do next.
"""
from __future__ import annotations

from app import constants as C
from app.candidates import Candidate

SUGGESTED_ACTIONS = {
    C.CAT_NO_CANDIDATE:
        "Check whether the bank settlement has not yet posted, or search the bank file manually for a "
        "record with this amount in a wider date window.",
    C.CAT_MULTIPLE_CANDIDATES:
        "Manually compare the listed candidates against the original payment notification / customer "
        "communication to pick the correct one.",
    C.CAT_AMOUNT_MISMATCH:
        "Verify with the bank/PSP whether a fee, FX conversion, or partial settlement explains the "
        "difference before approving manually.",
    C.CAT_MISSING_FIELDS:
        "Fix the source record (re-export from the gateway/ERP) and re-run reconciliation for this payment.",
    C.CAT_DUPLICATE:
        "Confirm with the bank whether the duplicate posting is a genuine double-charge; reverse/refund "
        "if so, otherwise mark the extra row as a bank duplicate.",
    C.CAT_CONFLICTING_EVIDENCE:
        "Escalate to finance ops -- one signal (usually the reference) says match, another (usually the "
        "amount) says no match. Requires manual investigation, not automation.",
    C.CAT_LOW_CONFIDENCE:
        "Review the candidate(s) manually; evidence was too weak (or the AI declined) to safely automate.",
    C.CAT_UNSUPPORTED_AI:
        "AI proposed a match that violated a hard policy cap (e.g. amount mismatch too large). Review the "
        "AI's reasoning and the policy reason before deciding manually.",
    C.CAT_AI_UNAVAILABLE:
        "Re-run reconciliation once the AI service is reachable, or resolve manually in the meantime.",
}


def build_exception_detail(
    category: str,
    reason: str,
    considered: list[Candidate],
    rejected: list[Candidate],
    duplicate_refs: list[str],
    ai_evidence: dict | None,
) -> dict:
    """Structured payload stored in decisions.evidence_json for an exception."""
    attempted = ["candidate_generation (settlement-window + amount-tolerance blocking, plus reference trace)"]
    if ai_evidence is not None:
        attempted.append("ai_semantic_review")

    return {
        "category": category,
        "category_label": C.CATEGORY_LABELS.get(category, category),
        "attempted": attempted,
        "evidence_found": {
            "candidates_considered": [c.to_evidence() for c in considered],
            "candidates_rejected": [c.to_evidence() for c in rejected],
            "duplicate_bank_refs": duplicate_refs,
        },
        "ai_evidence": ai_evidence,
        "why_unresolved": reason,
        "suggested_action": SUGGESTED_ACTIONS.get(category, "Review manually."),
    }

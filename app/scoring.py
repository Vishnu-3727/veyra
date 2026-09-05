"""Deterministic reconciliation decision tree.

Pure rule-based logic: given a payment's candidates (with precomputed
features), decide one of three outcomes:
  - AUTO_MATCH   : safe to reconcile automatically, no AI needed
  - NEEDS_AI     : genuinely ambiguous, escalate to AI reasoning
  - EXCEPTION    : cannot be safely resolved at all (with or without AI)

Every branch is explainable in plain language -- the `reason` string is
shown directly to the reviewer, so it must always state which evidence
drove the decision.

Candidate generation deliberately uses a loose amount/date blocking window
(so amount-mismatch and conflicting-evidence cases still surface as
candidates instead of vanishing). That means a raw candidate list can
contain "noise" -- records that coincidentally share a similar amount with
an unrelated customer. Before branching on candidate count, we separate
`plausible` candidates (real contenders: a strong reference trace, or a
decent amount+name combination) from noise, so noise never blocks a clean
match or inflates a genuine-ambiguity count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app import constants as C
from app.candidates import Candidate, find_duplicate_group
from config import Thresholds

NEEDS_AI = "NEEDS_AI"


@dataclass
class DeterministicOutcome:
    status: str  # AUTO_MATCH | NEEDS_AI | EXCEPTION
    category: Optional[str] = None
    matched: Optional[Candidate] = None
    reason: str = ""
    ai_candidates: list[Candidate] = field(default_factory=list)
    duplicate_refs: list[str] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    considered: list[Candidate] = field(default_factory=list)


def _is_exact_amount(c: Candidate, t: Thresholds) -> bool:
    return c.amount_diff_abs <= t.exact_amount_tolerance_abs or c.amount_diff_pct <= t.exact_amount_tolerance_pct


def _date_within_window(c: Candidate, t: Thresholds) -> bool:
    """Settlement date must fall within [0, settlement_window_days] of the payment date for a
    candidate to be auto-matched. An exact reference/amount can still surface a candidate whose
    date is outside this window (see app/candidates.py), but candidate generation and the
    auto-match decision are deliberately separate concerns: surfacing suspicious evidence is fine,
    approving it automatically is not. A reference collision (e.g. a reused/duplicate order
    token) plus an out-of-window date is exactly the conflicting-evidence case that should be
    reviewed, not silently trusted."""
    return 0 <= c.date_diff_days <= t.settlement_window_days


def _is_plausible(c: Candidate, t: Thresholds) -> bool:
    """A candidate is a real contender (not blocking noise) if it has a
    strong reference trace, or a good-enough amount+name combination."""
    if c.ref_match == "EXACT":
        return True
    if c.ref_match == "PARTIAL" and c.amount_diff_pct <= t.max_amount_mismatch_for_ai_pct:
        return True
    if _is_exact_amount(c, t) and c.name_sim >= t.min_name_similarity_for_ai:
        return True
    return False


def _single_candidate_outcome(c: Candidate, thresholds: Thresholds, considered, rejected, dup_refs) -> DeterministicOutcome:
    date_ok = _date_within_window(c, thresholds)
    strong_ref_amount = c.ref_match == "EXACT" and _is_exact_amount(c, thresholds)
    strong_amount_name = _is_exact_amount(c, thresholds) and c.name_sim >= thresholds.high_name_similarity

    if strong_ref_amount and date_ok:
        return DeterministicOutcome(
            status=C.STATUS_AUTO_MATCH, matched=c, duplicate_refs=dup_refs, considered=considered, rejected=rejected,
            reason="Exact reference match and exact amount, settled within the expected window -- no ambiguity.",
        )
    if strong_amount_name and date_ok:
        return DeterministicOutcome(
            status=C.STATUS_AUTO_MATCH, matched=c, duplicate_refs=dup_refs, considered=considered, rejected=rejected,
            reason=f"Exact amount and high name similarity ({c.name_sim}/100), settled within the expected "
                   f"window, uniquely identify this candidate.",
        )
    if c.ref_match == "EXACT" and c.amount_diff_pct > thresholds.ai_hard_amount_mismatch_cap_pct:
        return DeterministicOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_CONFLICTING_EVIDENCE, considered=considered, rejected=rejected,
            reason=f"Reference matches exactly but amount differs by {c.amount_diff_pct:.1%} "
                   f"(> {thresholds.ai_hard_amount_mismatch_cap_pct:.0%} safe cap) -- evidence conflicts.",
        )
    if (strong_ref_amount or strong_amount_name) and not date_ok:
        return DeterministicOutcome(
            status=NEEDS_AI, ai_candidates=[c], considered=considered, rejected=rejected,
            reason=f"Reference/amount/name evidence is otherwise clean, but the settlement date is "
                   f"{c.date_diff_days} day(s) from the payment date -- outside the "
                   f"{thresholds.settlement_window_days}-day settlement window, so this evidence conflicts with "
                   f"the expected timing and needs review rather than an automatic match.",
        )
    if c.amount_diff_pct <= thresholds.max_amount_mismatch_for_ai_pct and c.name_sim >= thresholds.min_name_similarity_for_ai:
        return DeterministicOutcome(
            status=NEEDS_AI, ai_candidates=[c], considered=considered, rejected=rejected,
            reason="Single plausible candidate but reference/amount/name evidence is not clean enough "
                   "for a deterministic auto-match; needs semantic review.",
        )
    if c.amount_diff_pct > thresholds.max_amount_mismatch_for_ai_pct:
        return DeterministicOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_AMOUNT_MISMATCH, considered=considered, rejected=rejected,
            reason=f"Amount differs by {c.amount_diff_pct:.1%} with no strong reference/name evidence to justify it.",
        )
    return DeterministicOutcome(
        status=C.STATUS_EXCEPTION, category=C.CAT_LOW_CONFIDENCE, considered=considered, rejected=rejected,
        reason=f"Weak evidence overall: reference_match={c.ref_match}, name_similarity={c.name_sim}/100.",
    )


def decide_deterministic(candidates: list[Candidate], thresholds: Thresholds) -> DeterministicOutcome:
    if not candidates:
        return DeterministicOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_NO_CANDIDATE,
            reason="No bank settlement record was found within the settlement window or via reference match.",
        )

    dup_group = find_duplicate_group(candidates)
    duplicate_extra_refs = [c.bank_ref for c in dup_group[1:]] if dup_group else []
    if dup_group:
        dup_set = {c.bank_ref for c in dup_group}
        effective = [dup_group[0]] + [c for c in candidates if c.bank_ref not in dup_set]
    else:
        effective = list(candidates)

    plausible = [c for c in effective if _is_plausible(c, thresholds)]
    noise = [c for c in effective if c not in plausible]

    if not plausible:
        # No candidate clears even the "real contender" bar; explain using the best overall candidate.
        top = effective[0]
        return _single_candidate_outcome(top, thresholds, effective, effective[1:], duplicate_extra_refs)

    if len(plausible) == 1:
        return _single_candidate_outcome(plausible[0], thresholds, effective, noise, duplicate_extra_refs)

    # Multiple plausible candidates.
    if len(plausible) > thresholds.max_candidates_for_ai:
        return DeterministicOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_MULTIPLE_CANDIDATES, considered=effective,
            reason=f"{len(plausible)} plausible candidates found -- too many to safely disambiguate automatically.",
        )

    top = plausible[0]
    if top.ref_match == "EXACT" and _is_exact_amount(top, thresholds) and _date_within_window(top, thresholds):
        rivals = [c for c in plausible[1:] if c.ref_match == "EXACT" and _is_exact_amount(c, thresholds)]
        if not rivals:
            return DeterministicOutcome(
                status=C.STATUS_AUTO_MATCH, matched=top, duplicate_refs=duplicate_extra_refs,
                rejected=plausible[1:] + noise, considered=effective,
                reason=f"Exact reference+amount match, settled within the expected window, clearly dominates "
                       f"{len(plausible) - 1} weaker plausible alternative(s).",
            )

    # Escalate only if at least one candidate is within an amount tolerance an AI could plausibly
    # justify -- but show the AI the FULL plausible set (including any far-off-amount candidate),
    # not just the amount-filtered subset. Otherwise a candidate holding the conflicting evidence
    # (e.g. exact reference but a huge amount mismatch) would be silently hidden from the AI instead
    # of being weighed as a competing, disqualifying signal.
    if not any(c.amount_diff_pct <= thresholds.max_amount_mismatch_for_ai_pct for c in plausible):
        return DeterministicOutcome(
            status=C.STATUS_EXCEPTION, category=C.CAT_MULTIPLE_CANDIDATES, considered=effective,
            reason=f"{len(plausible)} candidates found but none within an acceptable amount tolerance for review.",
        )
    return DeterministicOutcome(
        status=NEEDS_AI, ai_candidates=plausible, considered=effective,
        reason=f"{len(plausible)} plausible candidates with no single dominant match -- escalate to AI "
               f"to disambiguate.",
    )

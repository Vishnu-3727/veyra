"""Candidate generation and deterministic feature scoring.

For a payment, finds plausible bank-settlement candidates using a blocking
window (settlement date range + amount tolerance, OR a strong reference
trace regardless of amount -- so a matching reference with a wildly wrong
amount still surfaces as a candidate for the conflicting-evidence checks
rather than being silently dropped). All features here are facts computed
without any LLM call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from app.normalization import name_similarity, parse_date, ref_match_type
from config import Thresholds


@dataclass
class Candidate:
    bank_ref: str
    utr: str
    settlement_date: str
    amount: float
    narration: str
    payer_name: str
    reference_hint: str
    # computed features
    amount_diff_abs: float
    amount_diff_pct: float
    date_diff_days: int
    ref_match: str  # EXACT | PARTIAL | NONE
    name_sim: int

    def to_evidence(self) -> dict:
        return {
            "bank_ref": self.bank_ref,
            "utr": self.utr,
            "settlement_date": self.settlement_date,
            "amount": self.amount,
            "narration": self.narration,
            "payer_name": self.payer_name,
            "amount_diff_abs": round(self.amount_diff_abs, 2),
            "amount_diff_pct": round(self.amount_diff_pct, 4),
            "date_diff_days": self.date_diff_days,
            "ref_match": self.ref_match,
            "name_similarity": self.name_sim,
        }


def generate_candidates(
    payment: dict, bank_rows: list[dict], thresholds: Thresholds
) -> list[Candidate]:
    """Return plausible bank-record candidates for a payment, with features.

    Inclusion rule: within the settlement window AND within the amount
    blocking tolerance, OR has a strong reference trace (EXACT/PARTIAL)
    regardless of amount/date -- so a mismatched-amount fraud/error case
    still gets surfaced (and then rejected/escalated) rather than vanishing.
    """
    p_amount = payment.get("amount")
    p_date = parse_date(payment.get("created_at"))
    if p_amount is None or p_date is None:
        return []

    out: list[Candidate] = []
    for b in bank_rows:
        b_amount = b.get("amount")
        b_date = parse_date(b.get("settlement_date"))
        if b_amount is None or b_date is None:
            continue

        date_diff = (b_date - p_date).days
        amount_diff_abs = abs(b_amount - p_amount)
        amount_diff_pct = amount_diff_abs / p_amount if p_amount else 1.0

        ref_match = ref_match_type(
            payment.get("order_id", ""), payment.get("payment_id", ""),
            b.get("utr", ""), b.get("narration", ""), b.get("reference_hint", ""),
        )
        name_sim = name_similarity(payment.get("customer_name", ""), b.get("payer_name") or b.get("narration", ""))

        within_window = 0 <= date_diff <= thresholds.settlement_window_days
        within_amount = amount_diff_pct <= thresholds.candidate_amount_tolerance_pct
        strong_ref = ref_match in ("EXACT", "PARTIAL")
        plausible_name = name_sim >= thresholds.candidate_min_name_similarity

        if not ((within_window and within_amount and plausible_name) or (strong_ref and date_diff >= -1)):
            continue

        out.append(Candidate(
            bank_ref=b["bank_ref"], utr=b.get("utr", ""), settlement_date=b.get("settlement_date", ""),
            amount=b_amount, narration=b.get("narration", ""), payer_name=b.get("payer_name", ""),
            reference_hint=b.get("reference_hint", ""), amount_diff_abs=amount_diff_abs,
            amount_diff_pct=amount_diff_pct, date_diff_days=date_diff, ref_match=ref_match, name_sim=name_sim,
        ))

    # Best evidence first: exact ref, then lower amount diff, then higher name similarity.
    ref_rank = {"EXACT": 0, "PARTIAL": 1, "NONE": 2}
    out.sort(key=lambda c: (ref_rank[c.ref_match], c.amount_diff_pct, -c.name_sim))
    return out


def find_duplicate_group(candidates: list[Candidate], thresholds: Thresholds) -> list[Candidate]:
    """Detect candidates that are themselves duplicates of each other -- i.e.
    a bank double-post of the exact same underlying transaction, identified
    by a shared UTR. Coincidental similarity (same customer, same round
    amount, adjacent settlement dates) is deliberately NOT treated as a
    duplicate here -- that pattern is exactly what genuine multi-candidate
    ambiguity looks like, and collapsing it would hide real ambiguity from
    the decision layer instead of flagging a true duplicate.
    """
    if len(candidates) < 2:
        return []
    top = candidates[0]
    if not top.utr:
        return []
    dups = [top] + [c for c in candidates[1:] if c.utr == top.utr]
    return dups if len(dups) > 1 else []

"""Candidate generation and deterministic feature scoring.

For a payment, finds plausible bank-settlement candidates using a blocking
window (settlement date range + amount tolerance, OR a strong reference
trace regardless of amount -- so a matching reference with a wildly wrong
amount still surfaces as a candidate for the conflicting-evidence checks
rather than being silently dropped). All features here are facts computed
without any LLM call.

Inclusion rule (unchanged, and the contract every optimization below must preserve):

    A. date_diff in [0, settlement_window_days]
       AND amount_diff_pct <= candidate_amount_tolerance_pct
       AND name_sim >= candidate_min_name_similarity
    OR
    B. reference trace is EXACT or PARTIAL, AND date_diff >= -1
       (no amount/date-window constraint -- this is what keeps conflicting
        evidence visible instead of quietly filtered away)

Performance: the obvious implementation compares every payment against every
bank row, and pays for a reference normalization plus two fuzzy-string
comparisons on each pair -- O(P x B) fuzzy work, which is what made a
750-record batch take ~25s. `BankCandidateIndex` is built once per batch and
answers both branches without a full scan:

  * branch A -> settlement-date buckets, each kept sorted by amount, sliced
    with `bisect` to the amount-tolerance window; name similarity (the
    expensive part) is then computed only for rows already inside that window.
  * branch B -> an inverted index over character trigrams of each bank record's normalized
    reference text, queried with the trigrams of the payment's reference fragment.
    Completeness, stated precisely:
      - EXACT and suffix-PARTIAL traces are plain substring matches, so every trigram of the
        matched text is present in the index. These tiers are retrieved EXACTLY -- no misses.
        This is the tier that carries conflicting evidence (matching reference, wrong amount),
        so the safety-relevant case is the one with the hard guarantee.
      - the fuzzy tier (`partial_ratio >= 75` on a garbled/truncated reference) is retrieved by
        the same trigram lookup, which is a prefilter rather than a proof: a reference garbled
        with errors spread across every trigram of the fragment could in principle be missed.
        Realistic garbling (truncation, case/punctuation noise, a mangled span) leaves intact
        trigrams, and `tests/test_candidates.py` asserts indexed-vs-brute-force equivalence over
        full seeded datasets plus deliberately garbled references. `_generate_candidates_bruteforce`
        remains importable as the exact oracle for anyone auditing this trade-off.
      - fragments shorter than 9 characters have too few trigrams to filter usefully, so they
        fall back to a full scan.
    Retrieved rows are ALWAYS re-verified with the real matcher -- the index narrows what is
    checked, it never decides.

`_generate_candidates_bruteforce` states the inclusion rule directly and is the equivalence
oracle used by the tests, so an optimization here cannot silently change a reconciliation
decision without a test failing.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Union

from app.normalization import (
    name_similarity,
    normalized_bank_ref_text,
    parse_date,
    ref_fragment,
    ref_match_normalized,
    ref_match_type,
)
from config import Thresholds

_REF_GRAM = 3
# A fragment shorter than this yields too few trigrams to narrow anything usefully (and a
# 1-2 trigram query matches most rows anyway), so branch B just scans for those payments.
_MIN_FRAGMENT_FOR_GRAM_LOOKUP = 9


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


@dataclass(frozen=True)
class _BankRecord:
    """A bank row with its per-batch derived fields computed once."""

    position: int  # original row order -- the final sort tiebreak, kept stable
    bank_ref: str
    utr: str
    settlement_date: str
    amount: float
    narration: str
    payer_name: str
    reference_hint: str
    date: date
    ref_text: str   # normalized reference haystack (utr + narration + hint)
    name_text: str  # bank-side name used for similarity scoring


def _grams(text: str, size: int = _REF_GRAM) -> set[str]:
    """Character n-grams of `text`, skipping any that span the field separator."""
    return {
        text[i:i + size]
        for i in range(len(text) - size + 1)
        if " " not in text[i:i + size]
    }


class BankCandidateIndex:
    """Reusable per-batch index over bank settlement rows.

    Threshold-independent by design: the thresholds are applied at query time, so one index
    serves any threshold set (and a test can compare indexed vs brute-force output directly).
    """

    def __init__(self, bank_rows: Iterable[dict]):
        self.records: list[_BankRecord] = []
        self._by_date: dict[date, list[_BankRecord]] = {}
        self._amounts_by_date: dict[date, list[float]] = {}
        self._by_gram: dict[str, list[int]] = {}

        for position, b in enumerate(bank_rows):
            amount = b.get("amount")
            b_date = parse_date(b.get("settlement_date"))
            if amount is None or b_date is None:
                continue  # unusable as evidence; ingestion already flagged it invalid
            rec = _BankRecord(
                position=position,
                bank_ref=b["bank_ref"],
                utr=b.get("utr") or "",
                settlement_date=b.get("settlement_date") or "",
                amount=amount,
                narration=b.get("narration") or "",
                payer_name=b.get("payer_name") or "",
                reference_hint=b.get("reference_hint") or "",
                date=b_date,
                ref_text=normalized_bank_ref_text(
                    b.get("utr") or "", b.get("narration") or "", b.get("reference_hint") or "",
                ),
                name_text=(b.get("payer_name") or b.get("narration") or ""),
            )
            idx = len(self.records)
            self.records.append(rec)
            self._by_date.setdefault(rec.date, []).append(rec)
            for gram in _grams(rec.ref_text):
                self._by_gram.setdefault(gram, []).append(idx)

        for d, bucket in self._by_date.items():
            bucket.sort(key=lambda r: r.amount)
            self._amounts_by_date[d] = [r.amount for r in bucket]

    def in_date_window(self, start: date, window_days: int) -> Iterable[_BankRecord]:
        for offset in range(0, window_days + 1):
            yield from self._by_date.get(start + timedelta(days=offset), ())

    def in_date_amount_window(self, start: date, window_days: int, amount: float,
                              tolerance_pct: float) -> Iterable[_BankRecord]:
        """Rows settling within the date window whose amount is inside the tolerance band."""
        if amount is None or amount <= 0:
            # A zero/negative payment amount makes the percentage band meaningless; fall back to
            # the whole date bucket and let the caller's exact check decide.
            yield from self.in_date_window(start, window_days)
            return
        lo = amount * (1.0 - tolerance_pct) * (1 - 1e-9)
        hi = amount * (1.0 + tolerance_pct) * (1 + 1e-9)
        for offset in range(0, window_days + 1):
            d = start + timedelta(days=offset)
            bucket = self._by_date.get(d)
            if not bucket:
                continue
            amounts = self._amounts_by_date[d]
            for i in range(bisect_left(amounts, lo), bisect_right(amounts, hi)):
                yield bucket[i]

    def reference_prefilter(self, order_frag: str) -> Iterable[_BankRecord]:
        """Superset of the rows whose reference text can match `order_frag`."""
        if not order_frag:
            return ()
        if len(order_frag) < _MIN_FRAGMENT_FOR_GRAM_LOOKUP:
            return self.records
        hits: set[int] = set()
        for gram in _grams(order_frag):
            hits.update(self._by_gram.get(gram, ()))
        return [self.records[i] for i in hits]


def _feature_candidate(payment_amount: float, payment_date: date, payment_name: str,
                       order_frag: str, rec: _BankRecord) -> tuple[Candidate, int, float, int]:
    """Build the candidate plus the three inclusion facts, from cached record fields."""
    date_diff = (rec.date - payment_date).days
    amount_diff_abs = abs(rec.amount - payment_amount)
    amount_diff_pct = amount_diff_abs / payment_amount if payment_amount else 1.0
    ref_match = ref_match_normalized(order_frag, rec.ref_text)
    name_sim = name_similarity(payment_name, rec.name_text)
    candidate = Candidate(
        bank_ref=rec.bank_ref, utr=rec.utr, settlement_date=rec.settlement_date,
        amount=rec.amount, narration=rec.narration, payer_name=rec.payer_name,
        reference_hint=rec.reference_hint, amount_diff_abs=amount_diff_abs,
        amount_diff_pct=amount_diff_pct, date_diff_days=date_diff, ref_match=ref_match,
        name_sim=name_sim,
    )
    return candidate, date_diff, amount_diff_pct, name_sim


_REF_RANK = {"EXACT": 0, "PARTIAL": 1, "NONE": 2}


def _sorted(pairs: list[tuple[int, Candidate]]) -> list[Candidate]:
    """Best evidence first: exact ref, lower amount diff, higher name similarity, source order."""
    pairs.sort(key=lambda p: (_REF_RANK[p[1].ref_match], p[1].amount_diff_pct, -p[1].name_sim, p[0]))
    return [c for _, c in pairs]


def generate_candidates(
    payment: dict,
    bank_source: Union[list[dict], BankCandidateIndex],
    thresholds: Thresholds,
) -> list[Candidate]:
    """Return plausible bank-record candidates for a payment, with features.

    `bank_source` may be a raw list of bank rows (convenient for tests/CLI one-offs) or a
    prebuilt `BankCandidateIndex` (what the pipeline passes, built once per batch).
    """
    p_amount = payment.get("amount")
    p_date = parse_date(payment.get("created_at"))
    if p_amount is None or p_date is None:
        return []

    index = bank_source if isinstance(bank_source, BankCandidateIndex) else BankCandidateIndex(bank_source)
    p_name = payment.get("customer_name") or ""
    order_frag = ref_fragment(payment.get("order_id") or "")

    picked: dict[str, tuple[int, Candidate]] = {}

    # Branch A: normal blocking window (date + amount + plausible name).
    for rec in index.in_date_amount_window(
        p_date, thresholds.settlement_window_days, p_amount, thresholds.candidate_amount_tolerance_pct,
    ):
        candidate, date_diff, amount_diff_pct, name_sim = _feature_candidate(
            p_amount, p_date, p_name, order_frag, rec,
        )
        if (0 <= date_diff <= thresholds.settlement_window_days
                and amount_diff_pct <= thresholds.candidate_amount_tolerance_pct
                and name_sim >= thresholds.candidate_min_name_similarity):
            picked[rec.bank_ref] = (rec.position, candidate)

    # Branch B: strong reference trace, regardless of amount -- conflicting evidence must surface.
    for rec in index.reference_prefilter(order_frag):
        if rec.bank_ref in picked:
            continue
        candidate, date_diff, _, _ = _feature_candidate(p_amount, p_date, p_name, order_frag, rec)
        if candidate.ref_match in ("EXACT", "PARTIAL") and date_diff >= -1:
            picked[rec.bank_ref] = (rec.position, candidate)

    return _sorted(list(picked.values()))


def _generate_candidates_bruteforce(
    payment: dict, bank_rows: list[dict], thresholds: Thresholds
) -> list[Candidate]:
    """Naive reference implementation of the inclusion rule, kept as the equivalence oracle for
    `generate_candidates` (see tests/test_candidates.py). Not used in the pipeline: it is
    O(payments x bank rows) with fuzzy string work on every pair."""
    p_amount = payment.get("amount")
    p_date = parse_date(payment.get("created_at"))
    if p_amount is None or p_date is None:
        return []

    out: list[tuple[int, Candidate]] = []
    for position, b in enumerate(bank_rows):
        b_amount = b.get("amount")
        b_date = parse_date(b.get("settlement_date"))
        if b_amount is None or b_date is None:
            continue

        date_diff = (b_date - p_date).days
        amount_diff_abs = abs(b_amount - p_amount)
        amount_diff_pct = amount_diff_abs / p_amount if p_amount else 1.0

        ref_match = ref_match_type(
            payment.get("order_id", ""),
            b.get("utr", ""), b.get("narration", ""), b.get("reference_hint", ""),
        )
        name_sim = name_similarity(payment.get("customer_name", ""), b.get("payer_name") or b.get("narration", ""))

        within_window = 0 <= date_diff <= thresholds.settlement_window_days
        within_amount = amount_diff_pct <= thresholds.candidate_amount_tolerance_pct
        strong_ref = ref_match in ("EXACT", "PARTIAL")
        plausible_name = name_sim >= thresholds.candidate_min_name_similarity

        if not ((within_window and within_amount and plausible_name) or (strong_ref and date_diff >= -1)):
            continue

        out.append((position, Candidate(
            bank_ref=b["bank_ref"], utr=b.get("utr", ""), settlement_date=b.get("settlement_date", ""),
            amount=b_amount, narration=b.get("narration", ""), payer_name=b.get("payer_name", ""),
            reference_hint=b.get("reference_hint", ""), amount_diff_abs=amount_diff_abs,
            amount_diff_pct=amount_diff_pct, date_diff_days=date_diff, ref_match=ref_match, name_sim=name_sim,
        )))

    return _sorted(out)


def find_duplicate_group(candidates: list[Candidate]) -> list[Candidate]:
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

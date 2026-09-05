"""Tests for candidate generation, including the guarantee that the indexed
generator is behaviourally identical to the naive reference implementation.

The index exists purely for speed; if it ever returned a different candidate set it would
silently change reconciliation decisions, which is exactly the class of bug this file exists to
prevent. `_generate_candidates_bruteforce` is the oracle: it states the inclusion rule directly.
"""
import csv

import pytest

from app.candidates import (
    BankCandidateIndex,
    _generate_candidates_bruteforce,
    find_duplicate_group,
    generate_candidates,
)
from app.generate_dataset import generate
from config import THRESHOLDS


def bank(bank_ref, settlement_date, amount, *, utr="", narration="", payer_name="", reference_hint=""):
    return {
        "bank_ref": bank_ref, "utr": utr, "settlement_date": settlement_date, "amount": amount,
        "narration": narration, "payer_name": payer_name, "reference_hint": reference_hint,
    }


def payment(amount=1000.0, created_at="2026-01-01", order_id="order_abc123456", customer_name="Acme Traders"):
    return {"amount": amount, "created_at": created_at, "order_id": order_id, "customer_name": customer_name}


def refs(candidates):
    return [c.bank_ref for c in candidates]


def both_paths(p, rows):
    """Candidates from the indexed path and from the oracle, for direct comparison."""
    return generate_candidates(p, BankCandidateIndex(rows), THRESHOLDS), _generate_candidates_bruteforce(p, rows, THRESHOLDS)


def test_exact_reference_and_amount_in_window_is_a_candidate():
    rows = [bank("bnk_1", "2026-01-03", 1000.0, utr="UTR1", narration="NEFT/UTR1/ACME/order_abc123456",
                 payer_name="Acme Traders")]
    indexed, oracle = both_paths(payment(), rows)
    assert refs(indexed) == ["bnk_1"] == refs(oracle)
    assert indexed[0].ref_match == "EXACT"
    assert indexed[0].date_diff_days == 2


def test_settlement_outside_date_window_without_reference_is_not_a_candidate():
    rows = [bank("bnk_late", "2026-03-01", 1000.0, utr="UTR9", narration="NEFT/UTR9/ACME TRADERS",
                 payer_name="Acme Traders")]
    indexed, oracle = both_paths(payment(), rows)
    assert refs(indexed) == [] == refs(oracle)


def test_reference_match_outside_date_window_still_surfaces_as_candidate():
    """Conflicting evidence must stay visible: candidate generation surfaces it, and the
    decision layer (not this layer) refuses to auto-match it."""
    rows = [bank("bnk_ref", "2026-03-01", 1000.0, utr="UTR1", narration="NEFT/UTR1/order_abc123456")]
    indexed, oracle = both_paths(payment(), rows)
    assert refs(indexed) == ["bnk_ref"] == refs(oracle)
    assert indexed[0].date_diff_days == 59


def test_amount_far_outside_tolerance_still_surfaces_when_reference_matches():
    rows = [bank("bnk_conflict", "2026-01-02", 5000.0, utr="UTR1", narration="NEFT/UTR1/order_abc123456")]
    indexed, oracle = both_paths(payment(), rows)
    assert refs(indexed) == ["bnk_conflict"] == refs(oracle)
    assert indexed[0].amount_diff_pct == pytest.approx(4.0)


def test_amount_outside_tolerance_without_reference_is_dropped():
    rows = [bank("bnk_noise", "2026-01-02", 5000.0, utr="UTR7", narration="NEFT/UTR7/ACME TRADERS",
                 payer_name="Acme Traders")]
    indexed, oracle = both_paths(payment(), rows)
    assert refs(indexed) == [] == refs(oracle)


def test_name_floor_excludes_unrelated_customer_with_same_amount():
    rows = [bank("bnk_other", "2026-01-02", 1000.0, utr="UTR5", narration="NEFT/UTR5/ZENITH LOGISTICS",
                 payer_name="Zenith Logistics")]
    indexed, oracle = both_paths(payment(), rows)
    assert refs(indexed) == [] == refs(oracle)


def test_partial_reference_trace_is_classified_partial():
    rows = [bank("bnk_partial", "2026-01-02", 1000.0, utr="UTR2", narration="UPI/123456/ACME",
                 payer_name="Acme Traders")]
    indexed, oracle = both_paths(payment(order_id="order_abc123456"), rows)
    assert refs(indexed) == refs(oracle) == ["bnk_partial"]
    assert indexed[0].ref_match == "PARTIAL"


def test_short_reference_fragment_falls_back_to_full_scan_and_still_matches():
    """Fragments too short for the trigram pigeonhole bound must still be found."""
    rows = [bank("bnk_short", "2026-01-02", 1000.0, utr="AB12", narration="NEFT/AB12/ACME")]
    p = payment(order_id="AB12")
    indexed, oracle = both_paths(p, rows)
    assert refs(indexed) == ["bnk_short"] == refs(oracle)


def test_rows_with_unparseable_amount_or_date_are_skipped_by_both_paths():
    rows = [
        bank("bnk_bad_amount", "2026-01-02", None, utr="UTR1", narration="order_abc123456"),
        bank("bnk_bad_date", None, 1000.0, utr="UTR1", narration="order_abc123456"),
        bank("bnk_ok", "2026-01-02", 1000.0, utr="UTR1", narration="order_abc123456"),
    ]
    indexed, oracle = both_paths(payment(), rows)
    assert refs(indexed) == ["bnk_ok"] == refs(oracle)


def test_payment_missing_amount_or_date_yields_no_candidates():
    rows = [bank("bnk_1", "2026-01-02", 1000.0, utr="UTR1", narration="order_abc123456")]
    assert generate_candidates({"amount": None, "created_at": "2026-01-01"}, BankCandidateIndex(rows), THRESHOLDS) == []
    assert generate_candidates({"amount": 1000.0, "created_at": None}, BankCandidateIndex(rows), THRESHOLDS) == []


def test_duplicate_utr_rows_are_grouped_as_a_bank_double_post():
    rows = [
        bank("bnk_1", "2026-01-02", 1000.0, utr="UTR1", narration="NEFT/UTR1/order_abc123456"),
        bank("bnk_2", "2026-01-03", 1000.0, utr="UTR1", narration="NEFT/UTR1/order_abc123456"),
    ]
    indexed, oracle = both_paths(payment(), rows)
    assert refs(indexed) == refs(oracle)
    assert {c.bank_ref for c in find_duplicate_group(indexed)} == {"bnk_1", "bnk_2"}


def test_raw_row_list_and_prebuilt_index_are_interchangeable():
    rows = [bank("bnk_1", "2026-01-02", 1000.0, utr="UTR1", narration="NEFT/UTR1/order_abc123456")]
    p = payment()
    assert [c.to_evidence() for c in generate_candidates(p, rows, THRESHOLDS)] == \
           [c.to_evidence() for c in generate_candidates(p, BankCandidateIndex(rows), THRESHOLDS)]


@pytest.mark.parametrize("narration", [
    "NEFT/UTR9/order_abc123456",       # intact reference
    "NEFT/UTR9/ORDER-ABC-123-456",     # punctuation noise
    "NEFT/UTR9/orderabc123456extra",   # embedded in a longer token
    "UPI/abc123456/ACME",              # prefix stripped
    "UPI/abc12345/ACME",               # one trailing char lost
    "UPI/abc123X56/ACME",              # one character corrupted mid-fragment
    "UPI/bc123456/ACME",               # one leading char lost
    "UPI/a-b-c-1-2-3-4-5-6/ACME",      # separator noise between every character
    "UPI/ac13245/ACME",                # heavily garbled
    "UPI/999999/ACME",                 # unrelated reference
    "NEFT/UTR9/ZENITH LOGISTICS",      # no reference at all
])
def test_garbled_references_agree_between_indexed_and_bruteforce(narration):
    """Probes the fuzzy tier of the trigram prefilter: whatever the naive matcher concludes for a
    mangled reference, the indexed path must conclude too (including "not a candidate")."""
    rows = [bank("bnk_1", "2026-02-20", 4321.0, utr="UTR9", narration=narration, payer_name="Acme Traders")]
    p = payment(amount=1000.0)  # amount far outside tolerance -> only a reference trace can qualify
    indexed, oracle = both_paths(p, rows)
    assert [c.to_evidence() for c in indexed] == [c.to_evidence() for c in oracle], narration


@pytest.mark.parametrize("size", [200, 750])
def test_indexed_and_bruteforce_agree_on_a_full_seeded_dataset(tmp_path, size):
    """The equivalence proof that matters: identical candidate sets, features and ordering for
    every payment of a real generated dataset."""
    generate(42, size, tmp_path)

    def rows(name):
        with open(tmp_path / name, newline="") as f:
            out = []
            for r in csv.DictReader(f):
                r["amount"] = float(r["amount"]) if r["amount"] else None
                out.append(r)
            return out

    payments = rows("payments.csv")
    banks = rows("bank_settlements.csv")
    index = BankCandidateIndex(banks)

    for p in payments:
        indexed = generate_candidates(p, index, THRESHOLDS)
        oracle = _generate_candidates_bruteforce(p, banks, THRESHOLDS)
        assert [c.to_evidence() for c in indexed] == [c.to_evidence() for c in oracle], p["payment_id"]

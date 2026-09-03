"""Deterministic normalization utilities.

Everything here is pure, dependency-light, and testable in isolation --
these are the "facts" the system establishes without any LLM involvement:
parsing amounts/dates safely, normalizing names and references, and scoring
textual similarity.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from rapidfuzz import fuzz

_LEGAL_SUFFIXES = [
    "private limited", "pvt ltd", "pvt. ltd.", "p ltd", "(p) ltd",
    "limited", "llp", "ltd", "inc", "incorporated", "corp",
]
_SUFFIX_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in sorted(_LEGAL_SUFFIXES, key=len, reverse=True)) + r")\b\.?",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def normalize_name(name: Optional[str]) -> str:
    """Lowercase, strip legal suffixes/punctuation, collapse whitespace.

    Used to compare entity names across sources that use different legal
    naming conventions (e.g. "Acme Retail Pvt Ltd" vs "ACME RETAIL").
    """
    if not name:
        return ""
    s = name.lower()
    s = _SUFFIX_RE.sub(" ", s)
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def normalize_ref(value: Optional[str]) -> str:
    """Uppercase, alphanumeric-only representation of a reference string."""
    if not value:
        return ""
    return "".join(c for c in value.upper() if c.isalnum())


def name_similarity(a: Optional[str], b: Optional[str]) -> int:
    """Symmetric fuzzy similarity (0-100) between two normalized names."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0
    return round(fuzz.token_sort_ratio(na, nb))


def parse_amount(value) -> Optional[float]:
    """Safely parse a monetary amount. Returns None for missing/corrupt input.

    Never raises -- callers treat None as "amount unavailable", which is a
    validation failure, not a crash.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value != value:  # NaN
            return None
        return round(float(value), 2)
    s = str(value).strip()
    if s == "" or s.lower() in {"nan", "none", "null"}:
        return None
    s = re.sub(r"[,\u20b9$\s]", "", s)
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def parse_date(value) -> Optional[date]:
    """Parse a date/datetime string in any of a few common formats."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.date() if isinstance(value, datetime) else value
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def ref_fragment(reference_id: str, min_len: int = 4) -> str:
    """Normalized reference with common prefixes stripped, for substring matching."""
    r = normalize_ref(reference_id)
    for prefix in ("ORDER", "PAY", "ORD", "INV"):
        if r.startswith(prefix):
            r = r[len(prefix):]
            break
    return r if len(r) >= min_len else r


def ref_match_type(order_id: str, payment_id: str, *bank_texts: str) -> str:
    """Classify how strongly a payment's identifiers appear in bank-side text.

    Returns "EXACT", "PARTIAL", or "NONE". EXACT requires the full order
    reference fragment to appear verbatim (post-normalization) in some bank
    field. PARTIAL means a meaningful fuzzy trace was found (garbled/truncated
    reference) but not a clean containment match.
    """
    order_frag = ref_fragment(order_id)
    combined = " ".join(normalize_ref(t) for t in bank_texts if t)
    if not order_frag or not combined:
        return "NONE"
    if order_frag in combined:
        return "EXACT"
    # partial: trailing fragment (e.g. last 6 chars) present, or high partial-ratio
    if len(order_frag) >= 6 and order_frag[-6:] in combined:
        return "PARTIAL"
    if fuzz.partial_ratio(order_frag, combined) >= 75:
        return "PARTIAL"
    return "NONE"

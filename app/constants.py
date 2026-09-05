"""Shared status/category vocabulary used across the engine, API, and dashboard."""

# Terminal decision statuses
STATUS_AUTO_MATCH = "AUTO_MATCH"              # deterministic rule matched -- no AI involved
STATUS_AI_ASSISTED_MATCH = "AI_ASSISTED_MATCH"  # AI proposed a match AND it passed policy guardrails
STATUS_EXCEPTION = "EXCEPTION"                # unresolved -- requires human review

MATCHED_STATUSES = (STATUS_AUTO_MATCH, STATUS_AI_ASSISTED_MATCH)

# Audit-only status: a human reviewed an exception. It is NEVER a decision status -- the engine's
# decision row is untouched -- so it deliberately does not appear in MATCHED_STATUSES or in the
# decisions table's status vocabulary.
STATUS_EXCEPTION_REVIEWED = "EXCEPTION_REVIEWED"

# Run lifecycle. A run row is written at batch START as RUNNING, then moved to COMPLETED or
# FAILED. A process killed mid-batch therefore stays RUNNING -- honest, and never mistaken for a
# finished run by anything that summarizes or compares whole batches.
RUN_RUNNING = "RUNNING"
RUN_COMPLETED = "COMPLETED"
RUN_FAILED = "FAILED"

# Exception categories -- each maps to a human-readable explanation template
CAT_NO_CANDIDATE = "NO_CANDIDATE"
CAT_MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
CAT_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
CAT_MISSING_FIELDS = "MISSING_FIELDS"
CAT_DUPLICATE = "DUPLICATE_TRANSACTION"
CAT_CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
CAT_LOW_CONFIDENCE = "LOW_CONFIDENCE"
CAT_UNSUPPORTED_AI = "UNSUPPORTED_AI_DECISION"
CAT_AI_UNAVAILABLE = "AI_UNAVAILABLE"

CATEGORY_LABELS = {
    CAT_NO_CANDIDATE: "No matching candidate found",
    CAT_MULTIPLE_CANDIDATES: "Multiple plausible candidates",
    CAT_AMOUNT_MISMATCH: "Amount mismatch beyond tolerance",
    CAT_MISSING_FIELDS: "Missing or corrupt required field(s)",
    CAT_DUPLICATE: "Duplicate transaction detected",
    CAT_CONFLICTING_EVIDENCE: "Conflicting evidence across fields",
    CAT_LOW_CONFIDENCE: "Evidence too weak for a confident match",
    CAT_UNSUPPORTED_AI: "AI proposal rejected by policy guardrail",
    CAT_AI_UNAVAILABLE: "AI reasoning unavailable",
}

INVOICE_FOUND_CONSISTENT = "found_consistent"
INVOICE_FOUND_MISMATCH = "found_mismatch"
INVOICE_NOT_FOUND = "not_found"
INVOICE_AMBIGUOUS = "ambiguous"  # more than one invoice shares this payment's order_id
# An invoice row for this order exists but was unusable as evidence (corrupt amount/fields).
# Deliberately distinct from `not_found`: "no invoice" and "invoice we could not read" are
# different facts for a reviewer.
INVOICE_RECORD_INVALID = "record_invalid"

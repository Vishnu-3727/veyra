"""Central configuration: paths, environment wiring, and every financially
meaningful threshold used by the reconciliation engine.

Keeping thresholds here (instead of scattered magic numbers) makes the
decision logic auditable and easy to tune during the demo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a dev convenience only
    pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "finance.db"

RAW_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Thresholds:
    """All tunable decision knobs for candidate generation, deterministic
    auto-matching, AI escalation, and the hard guardrails placed on AI output.
    """

    # --- candidate generation (blocking window) ---
    settlement_window_days: int = 7  # bank settlement expected within [0, N] days of payment
    candidate_amount_tolerance_pct: float = 0.15  # up to 15% amount diff still considered a *candidate*
    candidate_min_name_similarity: int = 35  # weak-reference candidates also need some name overlap,
    # otherwise two unrelated customers who happen to pay the same round amount in the same window
    # would falsely look like ambiguous candidates for each other.

    # --- deterministic auto-match rules (no AI involved) ---
    exact_amount_tolerance_abs: float = 1.0  # absolute rupee tolerance treated as "exact"
    exact_amount_tolerance_pct: float = 0.005  # 0.5%
    high_name_similarity: int = 90  # rapidfuzz token_sort_ratio, 0-100

    # --- AI escalation band: only genuinely ambiguous cases reach the LLM ---
    min_name_similarity_for_ai: int = 55  # below this, name evidence is too weak to bother the LLM
    max_amount_mismatch_for_ai_pct: float = 0.06  # escalate amount mismatches <=6% to the LLM
    max_candidates_for_ai: int = 4  # more than this is too ambiguous -> straight to exception

    # --- policy guardrails enforced on AI output (AI can NEVER override these) ---
    ai_confidence_threshold: int = 75  # AI must self-report >=75/100 confidence to action a match
    ai_hard_amount_mismatch_cap_pct: float = 0.08  # hard ceiling regardless of AI's stated confidence

    # --- duplicate detection (deterministic) ---
    # Duplicate bank postings are identified by matching UTR (see app/candidates.py);
    # no fuzzy tolerance needed since a genuine double-post reuses the same UTR.

    # --- invoice corroboration (secondary evidence, not primary match target) ---
    invoice_amount_tolerance_pct: float = 0.01
    invoice_date_window_days: int = 10


THRESHOLDS = Thresholds()

# --- LLM wiring ---
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
AI_ENABLED = bool(LLM_API_KEY)

# --- API / dashboard wiring ---
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))
API_BASE_URL = os.getenv("API_BASE_URL", f"http://{API_HOST}:{API_PORT}")

# --- dataset generation ---
RANDOM_SEED = int(os.getenv("DATASET_SEED", "42"))
DATASET_SIZE = int(os.getenv("DATASET_SIZE", "750"))

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

    # --- reliability: stop hammering a broken/unreachable provider mid-batch ---
    # After this many consecutive AI failures (bad key, outage, network issue), the
    # rest of the batch's AI-eligible cases skip the network call entirely and go
    # straight to an AI_UNAVAILABLE exception -- so one bad key can't turn a 750-record
    # batch into a multi-minute hang waiting out the per-call timeout on every case.
    ai_circuit_breaker_threshold: int = 3

    # --- duplicate detection (deterministic) ---
    # Duplicate bank postings are identified by matching UTR (see app/candidates.py);
    # no fuzzy tolerance needed since a genuine double-post reuses the same UTR.

    # --- invoice corroboration (secondary evidence, not primary match target) ---
    invoice_amount_tolerance_pct: float = 0.01


THRESHOLDS = Thresholds()

# --- LLM wiring: these are only the SEED values read once at startup by
# app/settings.py. Once the process is running, the live provider/key/model
# are owned by app.settings (mutable, persisted to SQLite, changeable from
# the dashboard's Settings panel without a restart) -- read config.LLM_* or
# config.AI_ENABLED directly ONLY from app/settings.py itself.
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "nvidia/nemotron-nano-9b-v2:free")
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
AI_ENABLED = bool(LLM_API_KEY)

# How many AI-eligible cases are reasoned about at once. The AI escalation set is the only
# network-bound part of a run; at ~7s per call a 48-case batch costs ~5 minutes serially and
# well under a minute concurrently.
#
# The ceiling is the PROVIDER's rate limit, not ours, so the default is set for the provider
# this repo defaults to: OpenRouter's free tier, commonly ~20 requests/minute. At ~7s per call
# 3 in flight stays under that; 8 does not, and the 429s that follow are indistinguishable from
# an outage to the circuit breaker -- it would trip and disable AI for the rest of the batch.
# On a paid or direct endpoint (NVIDIA NIM, OpenAI, a local vLLM) raise this: 8-16 is fine and
# is where the latency win actually lives. 1 restores the original fully-serial behaviour.
AI_CONCURRENCY = max(1, int(os.getenv("AI_CONCURRENCY", "3")))

# Transport-level workaround for a common laptop/wifi failure mode, kept as an explicit knob
# rather than hidden magic. Some networks advertise an IPv6 default route but black-hole IPv6
# egress. `getaddrinfo` returns the provider's AAAA record first, and the installed HTTP client
# (httpx, via the openai SDK) tries addresses sequentially with the FULL connect timeout each --
# it has no Happy Eyeballs race, which is why `curl` succeeds where the SDK stalls. Measured on
# such a host: an identical request to the real provider took 40.1s dual-stack vs 0.04s pinned to
# IPv4. When true, the LLM HTTP client binds an IPv4 local address (forcing AF_INET); if that
# attempt fails with a connection error (i.e. an IPv6-only host with no IPv4 egress),
# app/ai_reasoning.py retries once with the unrestricted client, so neither world is broken.
# This is transport only -- it can never change a reconciliation decision.
LLM_FORCE_IPV4 = os.getenv("LLM_FORCE_IPV4", "1").strip().lower() not in ("0", "false", "no", "off")

# Whether a dashboard-entered API key is persisted to the `app_settings` table so it survives a
# restart. On by default (that is the point of the Settings panel). Set to 0 to keep the key out
# of data/finance.db entirely -- the file is a backup/bug-report artifact, and db.init_db() can
# only restrict its permissions, not encrypt it. (Named without "API_KEY" on purpose: the
# submission checker treats any non-empty *API_KEY*/*TOKEN*/*SECRET* slot in .env.example as a
# committed credential, and this is a boolean policy switch, not a credential.)
LLM_STORE_CREDENTIAL_ON_DISK = os.getenv("LLM_STORE_CREDENTIAL_ON_DISK", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

# Whether POST /settings may point the LLM client at an endpoint that is NOT one of the curated
# provider presets. Off by default: an HTTP-reachable settings endpoint that accepts any URL is
# a credential-exfiltration primitive (the stored key is sent to that URL as a bearer token) and
# an SSRF probe. Turn it on only for a deliberate custom/self-hosted endpoint (vLLM, Ollama,
# a corporate gateway); `.env`-supplied URLs are operator-owned and are never gated by this.
LLM_ALLOW_CUSTOM_ENDPOINT = os.getenv("LLM_ALLOW_CUSTOM_ENDPOINT", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

# --- API hardening: safe defaults for the local hackathon demo, but
# configurable via env for anything beyond localhost. ---
# Origins the dashboard is allowed to call the API from. The default is DERIVED from the port the
# dashboard is actually served on (run.sh exports DASHBOARD_PORT), because hardcoding 8501 while
# run.sh happily honors `DASHBOARD_PORT=8502 ./run.sh` produced a silent CORS failure: the
# browser origin and the allowed origin disagreed and every request from the dashboard was
# blocked. An explicit CORS_ALLOWED_ORIGINS always wins.
# DASHBOARD_PORT is validated as a port number because it is interpolated into that origin
# string: a value containing a comma would otherwise inject extra allowed origins.
_raw_dashboard_port = os.getenv("DASHBOARD_PORT", "8501").strip() or "8501"
if not (_raw_dashboard_port.isdigit() and 1 <= int(_raw_dashboard_port) <= 65535):
    raise ValueError(f"DASHBOARD_PORT must be a port number 1-65535, got {_raw_dashboard_port!r}")
DASHBOARD_PORT = _raw_dashboard_port
_DEFAULT_CORS_ORIGINS = f"http://127.0.0.1:{DASHBOARD_PORT},http://localhost:{DASHBOARD_PORT}"


def _parse_cors_origins(raw: str) -> list[str]:
    """Explicit origins only. `*` is rejected outright rather than silently honored: with auth
    disabled by default it would let any site on the internet read every financial endpoint, and
    it is the first thing an operator reaches for when a CORS error appears."""
    origins = []
    for o in raw.split(","):
        o = o.strip().rstrip("/")
        if not o:
            continue
        if o == "*":
            raise ValueError(
                "CORS_ALLOWED_ORIGINS='*' is refused: list the dashboard origins explicitly "
                "(e.g. http://127.0.0.1:8501,http://localhost:8501)."
            )
        if not o.startswith(("http://", "https://")):
            raise ValueError(f"CORS origin {o!r} must include a scheme (http:// or https://)")
        origins.append(o)
    return origins


CORS_ALLOWED_ORIGINS = _parse_cors_origins(os.getenv("CORS_ALLOWED_ORIGINS", _DEFAULT_CORS_ORIGINS))

# Optional shared-secret auth for the API. Empty (the default) disables auth
# entirely -- correct for the localhost hackathon demo. Set this (and send it
# as `X-API-Token` or `Authorization: Bearer <token>`) before exposing the API
# beyond localhost. Cross-site write protection does NOT depend on it: every
# mutating endpoint also rejects a foreign `Origin` header (see app/api.py).
API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "").strip()

# Expose FastAPI's interactive docs (/docs, /redoc, /openapi.json). Off by default: the full
# API surface is reconnaissance, and the dashboard never uses it.
API_ENABLE_DOCS = os.getenv("API_ENABLE_DOCS", "0").strip().lower() in ("1", "true", "yes", "on")

# --- dataset generation ---
RANDOM_SEED = int(os.getenv("DATASET_SEED", "42"))
DATASET_SIZE = int(os.getenv("DATASET_SIZE", "750"))
# Ceiling accepted by POST /dataset/generate. Set from measured behavior, not aspiration
# (scripts/benchmark.py, one core of an i5-1235U): 750 records reconcile in ~0.4s, 10,000 in
# ~36s, 20,000 in ~135s. 10,000 keeps an HTTP request from hanging for minutes while leaving
# real headroom; larger batches are still reachable from the CLI. Raise it only alongside a
# fresh benchmark.
MAX_DATASET_SIZE = int(os.getenv("MAX_DATASET_SIZE", "10000"))

"""AI-assisted reasoning for genuinely ambiguous reconciliation cases.

Scope is deliberately narrow: this module is only invoked for the minority
of payments where deterministic rules cannot safely auto-match (see
app/scoring.py). It never receives the full dataset, never decides
unilaterally, and its output is always re-validated by app/policy.py before
it can affect a financial decision.

If no API key is configured, or the call fails/times out/returns malformed
output, this returns an AIResult with decision="ERROR" -- callers must treat
that as "AI unavailable", never as an implicit match.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from app.candidates import Candidate

SYSTEM_PROMPT = """You are a financial reconciliation assistant helping match a payment \
transaction to the correct bank settlement record. You are given precomputed, trustworthy \
evidence (amount differences, date differences, reference match type, name similarity) -- \
do not invent additional facts.

Rules:
- Pick a candidate ONLY if the evidence, taken together, clearly supports it as the same \
underlying transaction as the payment.
- If two or more candidates are similarly plausible and nothing distinguishes them, or if \
the evidence conflicts (e.g. strong reference match but very different amount, or no \
candidate is well supported), you MUST decline by returning "NO_MATCH".
- A large, unexplained amount difference is disqualifying even if the name matches well.
- Respond with STRICT JSON only, matching this schema:
{"decision": "MATCH" or "NO_MATCH", "candidate_id": "<bank_ref or null>", "confidence": <0-100 integer>, \
"reasoning": "<one or two sentence explanation citing the evidence>", "risk_flags": ["<short flag>", ...]}
"""


@dataclass
class AIResult:
    decision: str  # "MATCH" | "NO_MATCH" | "ERROR"
    candidate_id: Optional[str] = None
    confidence: int = 0
    reasoning: str = ""
    risk_flags: list[str] = field(default_factory=list)
    error: Optional[str] = None
    latency_ms: float = 0.0
    model: str = ""

    def to_evidence(self) -> dict:
        return {
            "decision": self.decision, "candidate_id": self.candidate_id, "confidence": self.confidence,
            "reasoning": self.reasoning, "risk_flags": self.risk_flags, "error": self.error,
            "latency_ms": round(self.latency_ms, 1), "model": self.model,
        }


def _build_user_prompt(payment: dict, candidates: list[Candidate]) -> str:
    payload = {
        "payment": {
            "amount": payment.get("amount"),
            "created_at": payment.get("created_at"),
            "customer_name": payment.get("customer_name"),
            "order_id": payment.get("order_id"),
            "description": payment.get("description"),
        },
        "candidates": [
            {
                "candidate_id": c.bank_ref,
                "amount": c.amount,
                "amount_diff_abs": round(c.amount_diff_abs, 2),
                "amount_diff_pct": round(c.amount_diff_pct * 100, 2),
                "date_diff_days": c.date_diff_days,
                "reference_match": c.ref_match,
                "name_similarity_0_100": c.name_sim,
                "bank_narration": c.narration,
                "bank_payer_name": c.payer_name,
            }
            for c in candidates
        ],
    }
    return (
        "Evaluate which candidate (if any) is the correct match for this payment.\n\n"
        + json.dumps(payload, indent=2, default=str)
    )


def reason_about_candidates(payment: dict, candidates: list[Candidate]) -> AIResult:
    if not config.AI_ENABLED:
        return AIResult(decision="ERROR", error="AI disabled: no LLM_API_KEY configured", model=config.LLM_MODEL)

    start = time.perf_counter()
    try:
        from openai import OpenAI

        client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL, timeout=config.LLM_TIMEOUT_SECONDS)
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(payment, candidates)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        latency_ms = (time.perf_counter() - start) * 1000
        raw = resp.choices[0].message.content
        parsed = json.loads(raw)
        decision = str(parsed.get("decision", "")).upper()
        if decision not in ("MATCH", "NO_MATCH"):
            return AIResult(decision="ERROR", error=f"unrecognized AI decision value: {decision!r}",
                             latency_ms=latency_ms, model=config.LLM_MODEL)
        confidence = int(parsed.get("confidence", 0) or 0)
        return AIResult(
            decision=decision,
            candidate_id=parsed.get("candidate_id"),
            confidence=max(0, min(100, confidence)),
            reasoning=str(parsed.get("reasoning", "")),
            risk_flags=list(parsed.get("risk_flags", []) or []),
            latency_ms=latency_ms,
            model=config.LLM_MODEL,
        )
    except json.JSONDecodeError as e:
        return AIResult(decision="ERROR", error=f"malformed AI response (not valid JSON): {e}",
                         latency_ms=(time.perf_counter() - start) * 1000, model=config.LLM_MODEL)
    except Exception as e:  # noqa: BLE001 - any AI/network failure must degrade gracefully, never crash the pipeline
        return AIResult(decision="ERROR", error=f"AI call failed: {type(e).__name__}: {e}",
                         latency_ms=(time.perf_counter() - start) * 1000, model=config.LLM_MODEL)

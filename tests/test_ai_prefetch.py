"""Concurrency contract for the AI escalation batch (app.pipeline._prefetch_ai).

The AI-eligible cases of a run are reasoned about concurrently rather than one at a time.
That is a pure latency change: it must not alter which cases get a verdict, nor when the
circuit breaker decides the provider is broken. These pin both.
"""
import app.pipeline as pipeline
from app.ai_reasoning import AIResult
from config import THRESHOLDS


def _pending(n):
    return [(i, {"payment_id": f"pay_{i}"}, []) for i in range(n)]


def _patch(monkeypatch, verdicts):
    """Stub the LLM with a per-index script, recording call order."""
    calls = []

    def fake(payment, candidates, settings=None):
        idx = int(payment["payment_id"].split("_")[1])
        calls.append(idx)
        return verdicts[idx]

    monkeypatch.setattr(pipeline, "reason_about_candidates", fake)
    return calls


def test_every_case_gets_its_own_verdict(monkeypatch):
    verdicts = {i: AIResult(decision="MATCH", candidate_id=f"bank_{i}") for i in range(20)}
    calls = _patch(monkeypatch, verdicts)

    results, circuit_open, reason = pipeline._prefetch_ai(_pending(20), run_ai=None)

    assert not circuit_open and reason == ""
    assert len(calls) == 20, "every eligible case must be attempted exactly once"
    # Results are keyed by payment index, so concurrent completion cannot cross-wire a
    # verdict onto the wrong payment -- the failure this guards against is a silent
    # mismatched financial decision, not a crash.
    assert {i: r.candidate_id for i, r in results.items()} == {i: f"bank_{i}" for i in range(20)}


def test_breaker_trips_on_consecutive_failures_and_stops_calling(monkeypatch):
    threshold = THRESHOLDS.ai_circuit_breaker_threshold
    # Everything fails: the provider is down.
    verdicts = {i: AIResult(decision="ERROR", error="provider down") for i in range(100)}
    calls = _patch(monkeypatch, verdicts)

    results, circuit_open, reason = pipeline._prefetch_ai(_pending(100), run_ai=None)

    assert circuit_open and reason == "provider down"
    # The point of the breaker is not paying the per-call timeout 100 times. Concurrency
    # costs at most one in-flight chunk beyond the trip, and nothing more.
    assert len(calls) <= threshold + pipeline.config.AI_CONCURRENCY
    assert len(results) == len(calls)


def test_intermittent_failures_do_not_trip_the_breaker(monkeypatch):
    # Alternating fail/succeed never reaches `threshold` CONSECUTIVE failures, so a flaky
    # provider must not be mistaken for a dead one and abandon the rest of the batch.
    verdicts = {
        i: AIResult(decision="ERROR", error="flaky") if i % 2 else AIResult(decision="MATCH")
        for i in range(40)
    }
    calls = _patch(monkeypatch, verdicts)

    results, circuit_open, _ = pipeline._prefetch_ai(_pending(40), run_ai=None)

    assert not circuit_open
    assert len(calls) == 40 and len(results) == 40

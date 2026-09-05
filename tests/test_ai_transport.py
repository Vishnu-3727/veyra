"""LLM transport behavior.

Some networks advertise an IPv6 default route but black-hole IPv6 egress. `getaddrinfo` hands
back the provider's AAAA record first and the openai SDK's HTTP client tries addresses
sequentially with the full connect timeout each (no Happy Eyeballs), so a reachable provider
looked like a 20s timeout on every call. `config.LLM_FORCE_IPV4` pins the LLM client to IPv4.

These tests use a real localhost HTTP server -- no external network, no mocked sockets -- because
the thing worth defending is that the pinned client STILL WORKS. A pin that silently broke every
call would otherwise look identical to the bug it fixes.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import config
from app import ai_reasoning
from app import settings as llm_settings
from app.candidates import Candidate


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        answer = {"decision": "NO_MATCH", "candidate_id": None, "confidence": 10,
                  "reasoning": "localhost transport probe", "risk_flags": []}
        body = json.dumps({
            "id": "t", "object": "chat.completion", "created": 0, "model": "probe",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": json.dumps(answer)}}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def local_endpoint():
    """An OpenAI-compatible endpoint on 127.0.0.1 -- reachable over IPv4 only."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()
    srv.server_close()


def _candidate():
    return Candidate(bank_ref="bnk_1", utr="UTR1", settlement_date="2026-01-02", amount=1000.0,
                     narration="NEFT", payer_name="Acme", reference_hint="",
                     amount_diff_abs=0.0, amount_diff_pct=0.0, date_diff_days=1,
                     ref_match="NONE", name_sim=72)


def _settings(base_url):
    return llm_settings.LLMSettings(provider="custom", api_key="local-probe-not-a-secret",
                                    base_url=base_url, model="probe", timeout_seconds=10.0)


@pytest.mark.parametrize("force_ipv4", [True, False])
def test_llm_call_succeeds_with_and_without_the_ipv4_pin(local_endpoint, monkeypatch, force_ipv4):
    """The pin is a transport detail: a real request must complete either way, and the parsed
    decision must be identical. This is what stops the fix from becoming the next outage."""
    monkeypatch.setattr(config, "LLM_FORCE_IPV4", force_ipv4)
    payment = {"payment_id": "pay_1", "order_id": "order_x", "amount": 1000.0,
               "customer_name": "Acme Traders", "created_at": "2026-01-01T10:00:00"}

    result = ai_reasoning.reason_about_candidates(payment, [_candidate()],
                                                 settings=_settings(local_endpoint))

    assert result.decision == "NO_MATCH", result.error
    assert result.reasoning == "localhost transport probe"
    assert result.error is None


def test_the_pin_actually_restricts_the_address_family():
    """The pin must restrict the address family, not merely reorder it.

    Discriminating setup: a server listening on ::1 ONLY. An unpinned client reaches it; the
    IPv4-pinned client must not. Asserting both halves is the point -- a test that only checks
    "the pinned client failed" would pass even if the pin did nothing at all.
    """
    pinned = ai_reasoning._ipv4_http_client(5.0)
    if pinned is None:
        pytest.skip("no httpx-compatible transport installed")

    try:
        srv = ThreadingHTTPServer.__new__(ThreadingHTTPServer)
        srv.address_family = socket.AF_INET6
        ThreadingHTTPServer.__init__(srv, ("::1", 0), _Handler)
    except OSError:
        pytest.skip("host has no usable IPv6 loopback")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    hx = type(pinned)  # same client class the app uses, unpinned
    try:
        with hx(timeout=5.0) as unpinned:
            assert unpinned.post(f"http://[::1]:{port}/v1/chat/completions",
                                 json={}).status_code == 200, "control: IPv6 listener must be reachable"
        with pytest.raises(Exception):
            pinned.post(f"http://[::1]:{port}/v1/chat/completions", json={})
    finally:
        pinned.close()
        srv.shutdown()
        srv.server_close()

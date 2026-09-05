"""Suite-wide safety net: tests never talk to a real LLM provider.

Once a developer puts a real `LLM_API_KEY` in `.env`, `config.LLM_API_KEY` is populated for the
whole test session, and every test that runs a reconciliation batch would start issuing real
paid/rate-limited network calls. That was observed: the suite went from ~20s to ~140s, with
`tests/test_api.py` cases spending 5-20s each inside live provider requests. Live calls make
tests non-deterministic, dependent on a network, and able to burn a provider quota.

So AI is off by default for every test. A test that wants the AI path exercises it deliberately,
either by passing an explicit `LLMSettings` (see `tests/test_ai_transport.py`, which points at a
localhost server) or by calling `llm_settings.update(...)` itself.
"""
import pytest

import config
from app import settings as llm_settings


@pytest.fixture(autouse=True)
def _no_live_llm_calls(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(llm_settings, "_settings", None)
    yield
    monkeypatch.setattr(llm_settings, "_settings", None)

"""Tests for runtime LLM provider settings, especially key scoping across providers.

An API key is only valid for the provider that issued it, so carrying one across a provider
switch would produce 401s that look like a product failure. These tests pin that behavior.
"""
import pytest

import config

from app import db
from app import settings as llm_settings


@pytest.fixture()
def fresh_settings(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "LLM_API_KEY", "openrouter-key-1111")
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "nvidia/nemotron-nano-9b-v2:free")
    monkeypatch.setattr(config, "LLM_TIMEOUT_SECONDS", 20.0)
    monkeypatch.setattr(llm_settings, "_settings", None)
    db.init_db()
    yield
    monkeypatch.setattr(llm_settings, "_settings", None)


def test_initial_load_seeds_from_environment_config(fresh_settings):
    s = llm_settings.get()
    assert s.provider == "openrouter"
    assert s.api_key == "openrouter-key-1111"
    assert s.model == "nvidia/nemotron-nano-9b-v2:free"
    assert s.enabled is True
    assert "openrouter-key-1111" not in str(s.public_dict())  # raw key is never exposed
    assert s.public_dict()["key_hint"] == "...1111"


def test_changing_model_within_the_same_provider_keeps_the_key(fresh_settings):
    s = llm_settings.update(provider="openrouter", model="nvidia/nemotron-3-super-120b-a12b:free")
    assert s.model == "nvidia/nemotron-3-super-120b-a12b:free"
    assert s.api_key == "openrouter-key-1111"
    assert s.enabled is True


def test_changing_only_the_model_with_no_provider_argument_keeps_the_key(fresh_settings):
    s = llm_settings.update(model="some-other-model")
    assert s.api_key == "openrouter-key-1111"
    assert s.provider == "openrouter"


def test_changing_provider_without_a_new_key_clears_the_key_and_disables_ai(fresh_settings):
    """CASE 8 from the adversarial list: a key issued by one provider must not silently become
    the credential for another."""
    s = llm_settings.update(provider="groq")
    assert s.provider == "groq"
    assert s.api_key == ""
    assert s.enabled is False
    assert s.base_url == llm_settings.PROVIDER_PRESETS["groq"]["base_url"]
    assert s.key_hint is None


def test_changing_provider_with_an_explicit_key_uses_that_key(fresh_settings):
    s = llm_settings.update(provider="groq", api_key="groq-key-2222")
    assert s.provider == "groq"
    assert s.api_key == "groq-key-2222"
    assert s.enabled is True


def test_explicit_empty_key_clears_it_within_the_same_provider(fresh_settings):
    s = llm_settings.update(api_key="")
    assert s.provider == "openrouter"
    assert s.api_key == ""
    assert s.enabled is False


def test_settings_persist_across_a_process_restart(fresh_settings, monkeypatch):
    llm_settings.update(provider="groq", api_key="groq-key-3333", model="llama-3.1-8b-instant")
    monkeypatch.setattr(llm_settings, "_settings", None)  # simulate a fresh process
    s = llm_settings.get()
    assert s.provider == "groq"
    assert s.api_key == "groq-key-3333"
    assert s.model == "llama-3.1-8b-instant"


def test_update_is_the_first_settings_call_without_deadlocking(fresh_settings):
    """Regression guard: `update()` re-enters the module lock via `get()`, which deadlocked with
    a non-reentrant Lock when /settings was the first settings-related call of the process."""
    s = llm_settings.update(provider="openrouter", api_key="or-key-4444")
    assert s.provider == "openrouter"
    assert s.enabled is True


def test_an_empty_stored_key_does_not_mask_the_key_in_env(fresh_settings, monkeypatch):
    """A cleared key row must mean "unset", not "the key is the empty string".

    Clearing the key in the Settings panel writes an empty `llm_api_key` row. If that row won
    over the `.env` seed, then adding LLM_API_KEY to `.env` and restarting would leave AI
    disabled with nothing on screen to explain why -- the operator's configuration silently
    ignored.
    """
    llm_settings.update(provider="openrouter", api_key="")
    monkeypatch.setattr(llm_settings, "_settings", None)  # simulate a process restart

    s = llm_settings.get()

    assert s.api_key == "openrouter-key-1111"
    assert s.enabled is True


def test_env_key_is_not_reused_for_a_different_stored_provider(fresh_settings, monkeypatch):
    """The env fallback must stay provider-scoped: an OpenRouter key must never be sent to Groq
    just because the stored key is empty."""
    llm_settings.update(provider="groq")  # provider change clears the stored key
    monkeypatch.setattr(llm_settings, "_settings", None)

    s = llm_settings.get()

    assert s.provider == "groq"
    assert s.api_key == ""
    assert s.enabled is False


def test_env_base_url_is_matched_to_its_provider_preset(fresh_settings, monkeypatch):
    """A `.env` base URL must resolve to the preset that owns it, not to "custom".

    Keys are provider-scoped, so mislabelling an NVIDIA NIM URL as "custom" meant a key supplied
    purely through `.env` was never adopted for the nvidia_nim provider: AI stayed disabled and
    the run's recorded provider was wrong for auditing.
    """
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "nim-key-placeholder")
    monkeypatch.setattr(llm_settings, "_settings", None)

    s = llm_settings.get()

    assert s.provider == "nvidia_nim"
    assert s.api_key == "nim-key-placeholder"
    assert s.enabled is True


def test_an_unknown_base_url_is_still_custom(fresh_settings, monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://llm.internal.example.com/v1")
    monkeypatch.setattr(llm_settings, "_settings", None)

    assert llm_settings.get().provider == "custom"

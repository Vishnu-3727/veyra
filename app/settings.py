"""Runtime-configurable LLM provider settings.

Every other threshold in this project lives in config.py as a fixed,
auditable constant -- deliberately. LLM provider/key/model is the one
exception: a demo operator or judge may want to plug in whatever they
already have a subscription to (OpenAI, OpenRouter, NVIDIA NIM, Groq...) or
switch to a different open-weight model, without editing files or
restarting the process.

Settings live in-memory (a process-wide singleton) and are persisted to the
`app_settings` SQLite table so they survive a restart, seeded from
config.py's environment-variable defaults on first boot.

The API key is never returned by any read path -- only whether one is
configured and a masked last-4-character hint.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Optional

import config
from app import db

# Curated presets. All are OpenAI-compatible chat-completions endpoints, so
# ai_reasoning.py needs zero provider-specific code -- only base_url/model
# change. "openrouter" is the default: NVIDIA's open-weight Nemotron models
# are available there on a free tier with just a signup, no billing.
PROVIDER_PRESETS: dict[str, dict] = {
    "openrouter": {
        "label": "OpenRouter (Nemotron, free tier)",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "nvidia/nemotron-nano-9b-v2:free",
        "models": ["nvidia/nemotron-nano-9b-v2:free", "nvidia/nemotron-3-super-120b-a12b:free"],
        "key_url": "https://openrouter.ai/keys",
    },
    "nvidia_nim": {
        "label": "NVIDIA NIM (Nemotron)",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "default_model": "nvidia/llama-3.1-nemotron-70b-instruct",
        "models": ["nvidia/llama-3.1-nemotron-70b-instruct", "nvidia/nemotron-3-super-120b-a12b"],
        "key_url": "https://build.nvidia.com",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        "key_url": "https://platform.openai.com/api-keys",
    },
    "groq": {
        "label": "Groq (fast inference)",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
        "key_url": "https://console.groq.com/keys",
    },
    "custom": {
        "label": "Custom OpenAI-compatible endpoint",
        "base_url": "",
        "default_model": "",
        "models": [],
        "key_url": "",
    },
}
_SETTINGS_KEYS = ("llm_provider", "llm_api_key", "llm_base_url", "llm_model")


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def key_hint(self) -> Optional[str]:
        if not self.api_key:
            return None
        return f"...{self.api_key[-4:]}" if len(self.api_key) > 4 else "...."

    def public_dict(self) -> dict:
        """Everything EXCEPT the raw key -- safe to return from an API response."""
        return {
            "provider": self.provider, "base_url": self.base_url, "model": self.model,
            "enabled": self.enabled, "key_hint": self.key_hint,
        }


_lock = threading.Lock()
_settings: Optional[LLMSettings] = None


def _seed_from_env() -> LLMSettings:
    return LLMSettings(
        provider="openrouter" if "openrouter" in config.LLM_BASE_URL else "custom",
        api_key=config.LLM_API_KEY,
        base_url=config.LLM_BASE_URL,
        model=config.LLM_MODEL,
        timeout_seconds=config.LLM_TIMEOUT_SECONDS,
    )


def _load_persisted() -> Optional[dict]:
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(_SETTINGS_KEYS))})",
                _SETTINGS_KEYS,
            ).fetchall()
    except Exception:
        return None
    return {r["key"]: r["value"] for r in rows} if rows else None


def get() -> LLMSettings:
    """Return the current settings, loading from SQLite (or env defaults) on first call."""
    global _settings
    with _lock:
        if _settings is not None:
            return _settings
        seeded = _seed_from_env()
        persisted = _load_persisted()
        if persisted:
            seeded = replace(
                seeded,
                provider=persisted.get("llm_provider", seeded.provider),
                api_key=persisted.get("llm_api_key", seeded.api_key),
                base_url=persisted.get("llm_base_url", seeded.base_url),
                model=persisted.get("llm_model", seeded.model),
            )
        _settings = seeded
        return _settings


def update(provider: Optional[str] = None, api_key: Optional[str] = None,
           base_url: Optional[str] = None, model: Optional[str] = None) -> LLMSettings:
    """Apply a partial update and persist it. `api_key=None` keeps the
    existing key (so switching provider/model doesn't force re-entering a
    key); pass an empty string explicitly to clear it."""
    global _settings
    with _lock:
        cur = _settings or get()
        preset = PROVIDER_PRESETS.get(provider or cur.provider, PROVIDER_PRESETS["custom"])
        new = replace(
            cur,
            provider=provider or cur.provider,
            api_key=cur.api_key if api_key is None else api_key.strip(),
            base_url=(base_url or "").strip() or (preset["base_url"] if provider else "") or cur.base_url,
            model=(model or "").strip() or (preset["default_model"] if provider else "") or cur.model,
        )
        with db.get_conn() as conn:
            for k, v in zip(_SETTINGS_KEYS, (new.provider, new.api_key, new.base_url, new.model)):
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, v),
                )
        _settings = new
        return _settings

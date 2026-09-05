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
from urllib.parse import urlparse

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


_lock = threading.RLock()  # RLock: update() re-enters via get() while already holding the lock
_settings: Optional[LLMSettings] = None


def _provider_for_base_url(base_url: str) -> str:
    """Which preset a `.env`-supplied base URL belongs to.

    Matching against the preset table (rather than special-casing one vendor) is what lets a
    key configured purely in `.env` reach a supported provider: labelling an NVIDIA NIM URL as
    "custom" left the provider mislabelled in run metadata and, because keys are deliberately
    provider-scoped, stopped the env key from ever being adopted for that provider.
    """
    url = (base_url or "").strip().rstrip("/")
    for name, preset in PROVIDER_PRESETS.items():
        known = (preset["base_url"] or "").strip().rstrip("/")
        if known and url == known:
            return name
    return "custom"


# Hosts that must never be reachable from an API-supplied base URL: an operator-facing settings
# endpoint that accepts any URL turns the stored provider key into an exfiltration target and the
# server into an SSRF probe (cloud metadata, internal admin ports). Loopback/private ranges are
# only reachable when the operator explicitly opts in (`LLM_ALLOW_CUSTOM_ENDPOINT=1`), which is
# what a local vLLM/Ollama deployment needs.
_PRIVATE_HOST_PREFIXES = (
    "127.", "10.", "192.168.", "169.254.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
)
_PRIVATE_HOSTNAMES = ("localhost", "0.0.0.0", "::1", "[::1]", "metadata", "metadata.google.internal")


def preset_base_urls() -> set[str]:
    """Normalized base URLs of the curated presets -- the API's allowlist."""
    return {_normalize_url(p["base_url"]) for p in PROVIDER_PRESETS.values() if p["base_url"]}


def _normalize_url(url: Optional[str]) -> str:
    return (url or "").strip().rstrip("/")


def validate_api_base_url(base_url: str, provider: Optional[str]) -> str:
    """Gatekeeper for a base URL arriving over HTTP (POST /settings).

    Environment-seeded URLs bypass this deliberately: `.env` is operator-owned configuration,
    while an HTTP request may not be. A preset URL is always accepted. Anything else is a
    custom endpoint and requires BOTH an explicit `provider="custom"` selection and the
    `LLM_ALLOW_CUSTOM_ENDPOINT` opt-in, must be https, and must not point at loopback,
    link-local, or private address space.

    Raises ValueError with an operator-readable reason; the API turns that into a 400.
    """
    url = _normalize_url(base_url)
    if not url:
        return url
    if url in preset_base_urls():
        return url

    if provider != "custom":
        raise ValueError(
            f"base_url {url!r} is not one of the configured providers. Select provider='custom' "
            f"to use a different endpoint. Valid endpoints: {sorted(preset_base_urls())}"
        )
    if not config.LLM_ALLOW_CUSTOM_ENDPOINT:
        raise ValueError(
            "Custom LLM endpoints are disabled. Set LLM_ALLOW_CUSTOM_ENDPOINT=1 in the "
            "environment to allow this deployment to send its API key to a non-preset endpoint."
        )

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"base_url must use https, got {parsed.scheme or 'no'} scheme")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("base_url must include a hostname")
    if host in _PRIVATE_HOSTNAMES or host.startswith(_PRIVATE_HOST_PREFIXES):
        raise ValueError(
            f"base_url host {host!r} is loopback/private address space -- refused, because a "
            f"settings change must not be able to aim the configured API key at this host's "
            f"own network."
        )
    return url


def _seed_from_env() -> LLMSettings:
    return LLMSettings(
        provider=_provider_for_base_url(config.LLM_BASE_URL),
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
            stored_provider = persisted.get("llm_provider", seeded.provider)
            stored_key = persisted.get("llm_api_key") or ""
            # An EMPTY stored key means "no key configured", not "the key is the empty string".
            # Without this, a row written by clearing the key in the Settings panel would
            # permanently mask a key present in `.env`, and AI would stay disabled on the next
            # boot with nothing to explain why. The env key is only adopted when the stored
            # provider still matches the one `.env` describes, so this cannot smuggle a key
            # issued by one provider into requests aimed at another.
            if not stored_key and stored_provider == seeded.provider:
                stored_key = seeded.api_key
            seeded = replace(
                seeded,
                provider=stored_provider,
                api_key=stored_key,
                base_url=persisted.get("llm_base_url", seeded.base_url),
                model=persisted.get("llm_model", seeded.model),
            )
        _settings = seeded
        return _settings


def update(provider: Optional[str] = None, api_key: Optional[str] = None,
           base_url: Optional[str] = None, model: Optional[str] = None,
           trusted: bool = False) -> LLMSettings:
    """Apply a partial update and persist it.

    Key handling is deliberately scoped to the ENDPOINT the key was issued for, not merely to
    the provider label: an API key is only valid for the provider it came from, so changing the
    provider OR the base URL without supplying a new key clears the stored key (AI then falls
    back to the safe "unavailable" path until a key for the new endpoint is entered). Scoping on
    the label alone was exploitable -- a request supplying only `base_url` left `provider`
    unchanged, so the guard never fired and the next AI call sent the stored key, as a bearer
    token, to the caller's endpoint. Switching the MODEL within the same endpoint keeps the key.
    `api_key=None` means "not supplied"; an explicit empty string clears the key.

    `trusted=True` marks an operator-local caller (env seeding, CLI) and skips base-URL
    validation. Everything reaching this from HTTP MUST leave it False.
    """
    global _settings
    with _lock:
        cur = _settings or get()
        target_provider = provider or cur.provider
        if base_url and not trusted:
            base_url = validate_api_base_url(base_url, provider or cur.provider)
        preset = PROVIDER_PRESETS.get(target_provider, PROVIDER_PRESETS["custom"])
        target_base_url = (
            (base_url or "").strip() or (preset["base_url"] if provider else "") or cur.base_url
        )
        endpoint_changed = (
            target_provider != cur.provider
            or _normalize_url(target_base_url) != _normalize_url(cur.base_url)
        )
        if api_key is not None:
            new_key = api_key.strip()
        else:
            new_key = "" if endpoint_changed else cur.api_key
        new = replace(
            cur,
            provider=target_provider,
            api_key=new_key,
            base_url=target_base_url,
            model=(model or "").strip() or (preset["default_model"] if provider else "") or cur.model,
        )
        # `_load_persisted` already tolerates a missing table (first boot reads from `.env`), so
        # the writer must be equally self-sufficient rather than assuming the API created the
        # schema at startup. Idempotent, and settings writes are rare operator actions.
        #
        # The key is written in the clear (SQLite has no column encryption and inventing one
        # here would be theatre), which makes data/finance.db a credential store: db.init_db()
        # therefore restricts it to mode 0600. An operator who does not want the key on disk at
        # all sets LLM_STORE_CREDENTIAL_ON_DISK=0 -- the key then lives only in this process's
        # memory (and in `.env`, if that is where it came from) and a dashboard-entered key is
        # gone after a restart.
        persisted_key = new.api_key if config.LLM_STORE_CREDENTIAL_ON_DISK else ""
        db.init_db()
        with db.get_conn() as conn:
            for k, v in zip(_SETTINGS_KEYS, (new.provider, persisted_key, new.base_url, new.model)):
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, v),
                )
        _settings = new
        return _settings

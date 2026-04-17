from __future__ import annotations

import json
import os
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "models.json"

_FALLBACK_ROLE_MODELS: dict[str, str] = {
    "localize": "gemini/gemini-2.5-flash",
    "generate": "openai/gpt-5-mini",
    "feedback": "gemini/gemini-2.5-flash",
    "judge":    "gemini/gemini-2.5-pro",
}

_FALLBACK_ENV_OVERRIDES: dict[str, str] = {
    "localize": "SIEVE_MODEL_LOCALIZE",
    "generate": "SIEVE_MODEL_GENERATE",
    "feedback": "SIEVE_MODEL_FEEDBACK",
    "judge":    "SIEVE_MODEL_JUDGE",
}


def _load_config() -> tuple[dict[str, str], dict[str, str]]:
    if not _CONFIG_PATH.exists():
        return dict(_FALLBACK_ROLE_MODELS), dict(_FALLBACK_ENV_OVERRIDES)
    try:
        data = json.loads(_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return dict(_FALLBACK_ROLE_MODELS), dict(_FALLBACK_ENV_OVERRIDES)
    roles = dict(_FALLBACK_ROLE_MODELS)
    roles.update(data.get("sieve_roles", {}))
    envs = dict(_FALLBACK_ENV_OVERRIDES)
    envs.update(data.get("env_overrides", {}))
    return roles, envs


ROLE_MODELS, _ENV_OVERRIDES = _load_config()


def resolve_model(role: str, override: str | None = None) -> str:
    if override:
        return override
    env_var = _ENV_OVERRIDES.get(role)
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    if role not in ROLE_MODELS:
        raise ValueError(f"unknown role: {role!r}; known: {sorted(ROLE_MODELS)}")
    return ROLE_MODELS[role]

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

_DEFAULT_DRY_RUN_DIR = Path(__file__).resolve().parents[2] / "runs" / "dry_run"


def is_enabled() -> bool:
    return os.environ.get("SIEVE_DRY_RUN", "").strip() in {"1", "true", "TRUE", "yes"}


def _dir() -> Path:
    override = os.environ.get("SIEVE_DRY_RUN_DIR")
    return Path(override) if override else _DEFAULT_DRY_RUN_DIR


def dump(role: str, *, system: str | None, messages: list[dict], model: str) -> str:
    """Persist the rendered prompt to disk for inspection. Returns a deterministic stub id."""
    h = hashlib.sha256(json.dumps({"s": system or "", "m": messages, "mdl": model}, sort_keys=True).encode()).hexdigest()[:16]
    d = _dir() / role
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{h}.txt"
    if not p.exists():
        parts = [f"MODEL: {model}", ""]
        if system:
            parts.extend(["--- system ---", system, ""])
        for i, msg in enumerate(messages):
            parts.append(f"--- {msg.get('role', 'user')}[{i}] ---")
            parts.append(str(msg.get("content", "")))
            parts.append("")
        p.write_text("\n".join(parts))
    return h


STUB_TEXT = '{"dry_run": true}'

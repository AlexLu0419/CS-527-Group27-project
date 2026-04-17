from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / "runs" / "llm_cache"


def _cache_dir() -> Path:
    override = os.environ.get("SIEVE_LLM_CACHE_DIR")
    return Path(override) if override else _DEFAULT_CACHE_DIR


def _schema_hash(schema: dict | None) -> str:
    if not schema:
        return "none"
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]


def cache_key(
    *,
    model: str,
    system: str | None,
    messages: list[dict],
    temperature: float,
    seed: int | None,
    n: int,
    response_schema: dict | None,
) -> str:
    blob = json.dumps(
        {
            "m": model,
            "sys": system or "",
            "msgs": messages,
            "t": round(float(temperature), 6),
            "seed": seed,
            "n": n,
            "sch": _schema_hash(response_schema),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_cacheable(temperature: float, seed: int | None) -> bool:
    return float(temperature) == 0.0 or seed is not None


def _path_for(key: str) -> Path:
    d = _cache_dir() / key[:2]
    return d / f"{key}.json"


def load(key: str) -> Any | None:
    p = _path_for(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def save(key: str, value: Any) -> None:
    p = _path_for(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    tmp.replace(p)

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from sieve.llm import cache as llm_cache
from sieve.llm import dry_run
from sieve.llm.roles import resolve_model

log = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).resolve().parents[2] / "runs" / "llm_log"


@dataclass
class LLMResponse:
    text: str
    parsed: dict | None
    model: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    cache_hit: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _retryable_exceptions():
    try:
        from litellm.exceptions import (
            APIConnectionError,
            InternalServerError,
            RateLimitError,
            Timeout,
        )

        return (RateLimitError, Timeout, APIConnectionError, InternalServerError)
    except Exception:
        return (Exception,)


def _log_call(role: str, payload: dict) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        p = _LOG_DIR / f"{role}.jsonl"
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("llm log write failed: %s", e)


def _extract_json(text: str) -> dict | None:
    """Best-effort JSON extraction from a model response (handles ```json fences)."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
        # drop language tag on first line
        if s.startswith("json"):
            s = s[4:].lstrip()
    try:
        return json.loads(s)
    except Exception:
        # try first {...} block
        a = s.find("{")
        b = s.rfind("}")
        if a != -1 and b != -1 and b > a:
            try:
                return json.loads(s[a : b + 1])
            except Exception:
                return None
    return None


def _build_kwargs(
    *,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    n: int,
    seed: int | None,
    response_schema: dict | None,
) -> dict:
    kw: dict[str, Any] = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        n=n,
    )
    if seed is not None:
        kw["seed"] = seed
    if response_schema is not None:
        kw["response_format"] = {"type": "json_object"}
    return kw


def _call_litellm(kwargs: dict) -> Any:
    import litellm
    from litellm import completion

    litellm.drop_params = True

    retry_decorator = retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1.5, min=2, max=30),
        retry=retry_if_exception_type(_retryable_exceptions()),
    )
    return retry_decorator(completion)(**kwargs)


def _messages_with_system(system: str | None, messages: list[dict]) -> list[dict]:
    if not system:
        return messages
    if messages and messages[0].get("role") == "system":
        return messages
    return [{"role": "system", "content": system}, *messages]


def _schema_hint(response_schema: dict) -> str:
    return (
        "\n\nYou MUST output a single JSON object that validates against this JSON Schema. "
        "Do not wrap the JSON in markdown fences or commentary.\n\n"
        f"Schema:\n{json.dumps(response_schema, indent=2)}"
    )


def complete(
    *,
    role: str,
    messages: list[dict],
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    response_schema: dict | None = None,
    n: int = 1,
    seed: int | None = None,
    model: str | None = None,
) -> LLMResponse | list[LLMResponse]:
    """Unified LLM entry point. Returns a list when n > 1, else a single LLMResponse."""
    resolved = resolve_model(role, model)

    sys_text = system
    if response_schema is not None:
        sys_text = (sys_text or "") + _schema_hint(response_schema)
    msgs = _messages_with_system(sys_text, messages)

    # Dry run short-circuit
    if dry_run.is_enabled():
        stub_id = dry_run.dump(role, system=sys_text, messages=msgs, model=resolved)
        stub_parsed = None
        if response_schema is not None:
            stub_parsed = {"__dry_run__": True, "__stub_id__": stub_id}
        stub = LLMResponse(
            text=dry_run.STUB_TEXT,
            parsed=stub_parsed,
            model=resolved,
            usage={},
            latency_ms=0,
            cache_hit=False,
        )
        return [stub] * n if n > 1 else stub

    cache_enabled = llm_cache.is_cacheable(temperature, seed)
    key: str | None = None
    if cache_enabled:
        key = llm_cache.cache_key(
            model=resolved,
            system=sys_text,
            messages=messages,
            temperature=temperature,
            seed=seed,
            n=n,
            response_schema=response_schema,
        )
        cached = llm_cache.load(key)
        if cached is not None:
            out = [_dict_to_response(r, cache_hit=True) for r in cached]
            return out if n > 1 else out[0]

    start = time.perf_counter()
    kwargs = _build_kwargs(
        model=resolved,
        messages=msgs,
        temperature=temperature,
        max_tokens=max_tokens,
        n=n,
        seed=seed,
        response_schema=response_schema,
    )

    try:
        resp = _call_litellm(kwargs)
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        err = f"{type(e).__name__}: {e}"
        _log_call(role, {
            "ts": time.time(),
            "model": resolved,
            "messages": messages,
            "error": err,
            "latency_ms": latency_ms,
        })
        out = LLMResponse(text="", parsed=None, model=resolved, usage={}, latency_ms=latency_ms, cache_hit=False, error=err)
        return [out] * n if n > 1 else out

    latency_ms = int((time.perf_counter() - start) * 1000)
    choices = getattr(resp, "choices", None) or []
    usage_obj = getattr(resp, "usage", None)
    usage = {}
    if usage_obj is not None:
        usage = {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
            "completion_tokens": getattr(usage_obj, "completion_tokens", None),
            "total_tokens": getattr(usage_obj, "total_tokens", None),
        }

    results: list[LLMResponse] = []
    for idx, choice in enumerate(choices):
        text = _choice_text(choice)
        parsed = _extract_json(text) if response_schema is not None else None
        err: str | None = None
        if response_schema is not None and parsed is not None:
            try:
                jsonschema.validate(parsed, response_schema)
            except jsonschema.ValidationError as ve:
                # Single retry with validator error appended
                retry_msgs = msgs + [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            "Your previous output failed schema validation with: "
                            f"{ve.message}. Return a corrected JSON object that matches the schema. "
                            "Output only the JSON."
                        ),
                    },
                ]
                try:
                    retry_kwargs = _build_kwargs(
                        model=resolved,
                        messages=retry_msgs,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        n=1,
                        seed=seed,
                        response_schema=response_schema,
                    )
                    retry_resp = _call_litellm(retry_kwargs)
                    text = _choice_text(retry_resp.choices[0]) if retry_resp.choices else text
                    parsed = _extract_json(text)
                    if parsed is not None:
                        jsonschema.validate(parsed, response_schema)
                    else:
                        err = f"schema_retry_parse_failed: {ve.message}"
                except Exception as retry_exc:
                    err = f"schema_retry_failed: {ve.message} -> {retry_exc}"
                    parsed = None
        if response_schema is not None and parsed is None and err is None:
            err = "schema_parse_failed"
        results.append(
            LLMResponse(
                text=text,
                parsed=parsed,
                model=resolved,
                usage=usage,
                latency_ms=latency_ms,
                cache_hit=False,
                error=err,
            )
        )

    if not results:
        out = LLMResponse(text="", parsed=None, model=resolved, usage=usage, latency_ms=latency_ms, cache_hit=False, error="no_choices")
        return [out] * n if n > 1 else out

    _log_call(
        role,
        {
            "ts": time.time(),
            "model": resolved,
            "messages": messages,
            "system_preview": (sys_text or "")[:500],
            "responses": [r.to_dict() for r in results],
            "usage": usage,
            "latency_ms": latency_ms,
        },
    )

    if cache_enabled and key is not None and all(r.error is None for r in results):
        llm_cache.save(key, [r.to_dict() for r in results])

    return results if n > 1 else results[0]


def _choice_text(choice: Any) -> str:
    msg = getattr(choice, "message", None)
    if msg is None and isinstance(choice, dict):
        msg = choice.get("message")
    if msg is None:
        return ""
    content = getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "".join(parts)
    return str(content)


def _dict_to_response(d: dict, *, cache_hit: bool) -> LLMResponse:
    return LLMResponse(
        text=d.get("text", ""),
        parsed=d.get("parsed"),
        model=d.get("model", ""),
        usage=d.get("usage", {}) or {},
        latency_ms=d.get("latency_ms", 0),
        cache_hit=cache_hit,
        error=d.get("error"),
    )


# Suppress unused-import lint warnings for re-exported retries
_UNUSED = (RetryError, os)

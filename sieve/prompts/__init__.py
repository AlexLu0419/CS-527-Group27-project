from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined, Template

PROMPTS_DIR = Path(__file__).resolve().parent


@dataclass
class PromptSpec:
    id: str
    version: int
    role: str
    temperature: float
    system: str | None
    template: str
    output_schema: dict | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    def render(self, **ctx: Any) -> str:
        tmpl = Template(self.template, undefined=_LenientUndefined, keep_trailing_newline=True)
        return tmpl.render(**ctx)

    def messages(self, **ctx: Any) -> list[dict]:
        return [{"role": "user", "content": self.render(**ctx)}]

    def content_hash(self) -> str:
        blob = yaml.safe_dump(
            {
                "id": self.id,
                "version": self.version,
                "role": self.role,
                "temperature": self.temperature,
                "system": self.system,
                "template": self.template,
                "output_schema": self.output_schema,
                "extras": self.extras,
            },
            sort_keys=True,
            allow_unicode=True,
        )
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


# Relax: unknown keys render as empty instead of raising. StrictUndefined kept around for testing.
class _LenientUndefined(StrictUndefined):
    def _fail_with_undefined_error(self, *args, **kwargs):
        return ""


@lru_cache(maxsize=None)
def load_prompt(name: str) -> PromptSpec:
    path = PROMPTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    known = {"id", "version", "role", "temperature", "system", "template", "output_schema"}
    extras = {k: v for k, v in spec.items() if k not in known}
    return PromptSpec(
        id=spec["id"],
        version=int(spec.get("version", 1)),
        role=spec["role"],
        temperature=float(spec.get("temperature", 0.0)),
        system=spec.get("system"),
        template=spec["template"],
        output_schema=spec.get("output_schema"),
        extras=extras,
        source_path=path,
    )


__all__ = ["PromptSpec", "load_prompt", "PROMPTS_DIR"]

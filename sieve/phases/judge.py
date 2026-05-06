"""Layer 3 — LLM-as-a-Judge tiebreaker.

Grades a candidate patch on a 4-item binary rubric + per-test assessment,
computes weighted score client-side, and returns a structured JudgeOutput.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from sieve.judge.assemble import JudgeInput
from sieve.llm import complete
from sieve.llm.roles import resolve_model
from sieve.prompts import load_prompt

logger = logging.getLogger("sieve.phases.judge")

RUBRIC_WEIGHTS = {
    "root_cause": 3,
    "not_symptom_only": 3,
    "no_scope_creep": 2,
    "failing_tests_legitimate": 3,
}
_TOTAL_WEIGHT = sum(RUBRIC_WEIGHTS.values())  # 11


@dataclass
class JudgeOutput:
    rubric_scores: dict[str, int] = field(default_factory=dict)
    failing_test_assessments: list[dict] = field(default_factory=list)
    reasoning: str = ""
    verdict: str = "UNCERTAIN"           # ACCEPT | REJECT | UNCERTAIN
    confidence: str = "low"              # low | medium | high
    weighted_score: float = 0.0
    model: str = ""
    raw_response: str = ""
    parse_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def compute_weighted_score(rubric_scores: dict[str, int]) -> float:
    total = 0
    for key, weight in RUBRIC_WEIGHTS.items():
        total += weight * int(rubric_scores.get(key, 0))
    return total / _TOTAL_WEIGHT


def run_judge(judge_input: JudgeInput, *, model: str | None = None) -> JudgeOutput:
    spec = load_prompt("judge_patch")
    resolved_model = resolve_model(spec.role, model)
    ctx = dict(
        issue_text=judge_input.issue_text,
        unified_diff=judge_input.unified_diff,
        hunks=judge_input.hunks,
        failing_tests=judge_input.failing_tests,
        passing_tests=judge_input.passing_tests,
        context_lines=judge_input.context_lines,
        zero_signal=judge_input.zero_signal,
    )

    resp = complete(
        role=spec.role,
        system=spec.system,
        messages=spec.messages(**ctx),
        temperature=spec.temperature,
        response_schema=spec.output_schema,
        model=resolved_model,
    )
    if isinstance(resp, list):
        resp = resp[0]

    out = JudgeOutput(model=resp.model, raw_response=resp.text)
    if resp.parsed is None:
        out.parse_error = resp.error or "judge_no_json"
        return out

    rubric = resp.parsed.get("rubric_scores", {}) or {}
    # Coerce to ints in {0, 1}
    coerced = {k: int(v) if v in (0, 1, "0", "1") else 0 for k, v in rubric.items()}
    # Guarantee all 4 keys are present
    for k in RUBRIC_WEIGHTS:
        coerced.setdefault(k, 0)
    out.rubric_scores = coerced
    out.failing_test_assessments = resp.parsed.get("failing_test_assessments", []) or []
    out.reasoning = (resp.parsed.get("reasoning") or "").strip()
    verdict = (resp.parsed.get("verdict") or "UNCERTAIN").strip().upper()
    if verdict not in ("ACCEPT", "REJECT", "UNCERTAIN"):
        verdict = "UNCERTAIN"
    out.verdict = verdict
    confidence = (resp.parsed.get("confidence") or "low").strip().lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "low"
    out.confidence = confidence
    out.weighted_score = compute_weighted_score(coerced)
    return out

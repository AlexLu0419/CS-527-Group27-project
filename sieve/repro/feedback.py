"""Haiku-class scenario-description synthesizer (prompt pack §5)."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from sieve.llm import complete
from sieve.prompts import load_prompt
from sieve.repro.issue import IssueBundle

_ASSERT_RE = re.compile(r"AssertionError[:\s](.*)")


@dataclass
class Feedback:
    scenario: str
    conceptual_gap: str
    direction: str
    raw: str
    error: str | None = None

    def to_text(self) -> str:
        return "\n".join(
            line for line in (self.scenario, self.conceptual_gap, self.direction) if line
        ).strip()

    def to_dict(self) -> dict:
        return asdict(self)


def extract_assertion(stdout: str) -> str:
    m = _ASSERT_RE.search(stdout or "")
    if m:
        return m.group(1).strip()[:200]
    return ""


def synthesize_feedback(
    issue: IssueBundle,
    patch_diff: str,
    *,
    exit_code: int,
    stdout: str,
    stderr: str = "",
) -> Feedback:
    spec = load_prompt("feedback_synth")
    assertion_msg = extract_assertion(stdout)
    ctx = dict(
        one_line_summary=issue.one_line_summary or "(unknown)",
        expected_behavior=issue.expected or "(unknown)",
        actual_behavior=issue.actual or "(unknown)",
        patch_diff=patch_diff[:6000],
        exit_code=exit_code,
        stdout=(stdout or "")[-1500:],
        stderr=(stderr or "")[-500:],
        assertion_message=assertion_msg,
    )
    resp = complete(
        role=spec.role,
        system=spec.system,
        messages=spec.messages(**ctx),
        temperature=spec.temperature,
        response_schema=spec.output_schema,
    )
    if isinstance(resp, list):
        resp = resp[0]
    if resp.parsed:
        return Feedback(
            scenario=resp.parsed.get("scenario", "").strip(),
            conceptual_gap=resp.parsed.get("conceptual_gap", "").strip(),
            direction=resp.parsed.get("direction", "").strip(),
            raw=resp.text,
            error=None,
        )
    return Feedback(
        scenario="",
        conceptual_gap="",
        direction="",
        raw=resp.text,
        error=resp.error or "feedback_no_json",
    )

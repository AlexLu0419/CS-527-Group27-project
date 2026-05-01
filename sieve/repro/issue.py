"""Issue parser: extract structured fields from a SWE-bench issue.

LLM-first (prompt pack §1) with a regex sanity-check on the traceback field.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field

from sieve.llm import complete
from sieve.prompts import load_prompt

logger = logging.getLogger("sieve.repro.issue")

_BODY_MAX_CHARS = 8192

_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):(?:\n.+)+?\n([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning))",
    re.MULTILINE,
)


@dataclass
class IssueBundle:
    instance_id: str
    repo: str
    raw_text: str
    body: str
    one_line_summary: str = ""
    traceback: str | None = None
    exception_type: str | None = None
    reporter_snippet: str | None = None
    expected: str = ""
    actual: str = ""
    affected_modules: list[str] = field(default_factory=list)
    parse_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"\n... [truncated {len(text) - n} chars]"


def _extract_title_and_body(problem_statement: str) -> tuple[str, str]:
    """Heuristically split a SWE-bench problem_statement into (title, body).

    SWE-bench problem_statements typically lead with a single title-like line
    followed by the body. If no clear title is detectable we return ("", full).
    """
    if not problem_statement:
        return "", ""
    first, _, rest = problem_statement.partition("\n")
    first_stripped = first.strip()
    if first_stripped and len(first_stripped) < 200 and not first_stripped.endswith("."):
        return first_stripped, rest.lstrip("\n")
    return "", problem_statement


def parse_issue(instance: dict) -> IssueBundle:
    """Parse an issue into structured fields. Uses LLM per prompt pack §1."""
    instance_id = instance["instance_id"]
    repo = instance.get("repo", "")
    raw_text = instance.get("problem_statement", "") or ""
    title, body = _extract_title_and_body(raw_text)
    prompt_body = _truncate(body, _BODY_MAX_CHARS)

    bundle = IssueBundle(
        instance_id=instance_id,
        repo=repo,
        raw_text=raw_text,
        body=prompt_body,
    )

    if not raw_text.strip():
        bundle.parse_error = "empty_problem_statement"
        return bundle

    spec = load_prompt("issue_parser")
    messages = [
        {
            "role": "user",
            "content": spec.render(issue_title=title, issue_body=prompt_body),
        }
    ]
    resp = complete(
        role=spec.role,
        system=spec.system,
        messages=messages,
        temperature=spec.temperature,
        response_schema=spec.output_schema,
    )
    if isinstance(resp, list):
        resp = resp[0]

    parsed = resp.parsed if resp.parsed else None
    if parsed is None:
        bundle.parse_error = resp.error or "issue_parser_no_json"
        # Still try a regex fallback for traceback + exception type
        tb_match = _TRACEBACK_RE.search(raw_text)
        if tb_match:
            bundle.exception_type = tb_match.group(1)
            bundle.traceback = tb_match.group(0)
        return bundle

    bundle.one_line_summary = (parsed.get("one_line_summary") or "").strip()
    bundle.traceback = parsed.get("traceback") or None
    bundle.exception_type = parsed.get("exception_type") or None
    bundle.reporter_snippet = parsed.get("reporter_snippet") or None
    bundle.expected = (parsed.get("expected_behavior") or "").strip()
    bundle.actual = (parsed.get("actual_behavior") or "").strip()
    mods = parsed.get("affected_modules") or []
    if isinstance(mods, list):
        bundle.affected_modules = [str(m).strip() for m in mods if str(m).strip()]

    # Regex sanity check — if LLM missed a visible traceback, backfill it.
    if bundle.traceback is None:
        tb_match = _TRACEBACK_RE.search(raw_text)
        if tb_match:
            bundle.traceback = tb_match.group(0)
            if not bundle.exception_type:
                bundle.exception_type = tb_match.group(1)

    return bundle

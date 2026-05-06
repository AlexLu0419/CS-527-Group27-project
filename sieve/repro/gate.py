"""Buggy-repo gate: run candidates on unpatched repo, classify into A/B/C, drop junk.

Design:
- Drop: import/syntax/fixture errors — irrelevant to the issue.
- Bucket A (weight 1.0): assertion failure whose stdout shares ≥3 content words
  with the issue's expected/actual/body text.
- Bucket B (weight 0.7): specific exception matching issue.exception_type.
- Bucket C (weight 0.4): generic error consistent with the issue but weaker signal.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field

from sieve.phases._docker import docker_session
from sieve.repro.generate import Candidate
from sieve.repro.issue import IssueBundle
from sieve.repro.runner import run_scripts

logger = logging.getLogger("sieve.repro.gate")

_DROP_PATTERNS = [
    re.compile(r"\bModuleNotFoundError\b"),
    re.compile(r"\bImportError\b"),
    re.compile(r"\bSyntaxError\b"),
    re.compile(r"\bIndentationError\b"),
    re.compile(r"\bNameError: name '.+?' is not defined"),
    re.compile(r"fixture '.+?' not found"),
    re.compile(r"fixture .* not found"),
    re.compile(r"ERRORS\b.*while running .*conftest"),
]

_ASSERTION_MARKERS = [re.compile(r"\bAssertionError\b"), re.compile(r"^Issue reproduced$", re.MULTILINE)]

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_STOPWORDS = {
    "the", "and", "for", "this", "that", "with", "from", "have", "has", "are", "was",
    "not", "but", "can", "you", "your", "when", "should", "would", "could", "does",
    "does", "did", "issue", "bug", "expected", "actual", "then", "also",
}


@dataclass
class GatedTest:
    cand_id: str
    mask: str
    sample_idx: int
    source: str
    bucket: str            # "A" | "B" | "C" | "DROP"
    weight: float
    buggy_exit: int
    buggy_stdout_tail: str
    drop_reason: str | None = None
    match_words: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _content_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in _STOPWORDS}


def _issue_signal_words(issue: IssueBundle) -> set[str]:
    parts = [issue.expected, issue.actual, issue.one_line_summary]
    if issue.reporter_snippet:
        parts.append(issue.reporter_snippet)
    # limit body contribution to avoid washing out
    parts.append((issue.body or "")[:1000])
    return set().union(*(_content_words(p) for p in parts if p))


def _drop_reason(stdout: str) -> str | None:
    for pat in _DROP_PATTERNS:
        if pat.search(stdout):
            return pat.pattern
    return None


def _has_assertion_failure(stdout: str) -> bool:
    return any(p.search(stdout) for p in _ASSERTION_MARKERS)


def _has_exception(stdout: str, exc_name: str) -> bool:
    if not exc_name:
        return False
    return bool(re.search(rf"\b{re.escape(exc_name)}\b", stdout))


def classify_bucket(
    *,
    candidate: Candidate,
    exit_code: int,
    stdout: str,
    issue: IssueBundle,
) -> GatedTest:
    """Pure function: classify one test's buggy-repo run."""
    drop = _drop_reason(stdout) if exit_code != 0 else None
    if drop:
        return GatedTest(
            cand_id=candidate.cand_id,
            mask=candidate.mask,
            sample_idx=candidate.sample_idx,
            source=candidate.source,
            bucket="DROP",
            weight=0.0,
            buggy_exit=exit_code,
            buggy_stdout_tail=stdout[-500:],
            drop_reason=drop,
        )
    # A test that passes on buggy repo (exit 0, "Issue resolved") is useless —
    # drop it. On the buggy repo we need a failure signal.
    if exit_code == 0 and "Issue resolved" in stdout and "Issue reproduced" not in stdout:
        return GatedTest(
            cand_id=candidate.cand_id,
            mask=candidate.mask,
            sample_idx=candidate.sample_idx,
            source=candidate.source,
            bucket="DROP",
            weight=0.0,
            buggy_exit=exit_code,
            buggy_stdout_tail=stdout[-500:],
            drop_reason="passes_on_buggy_repo",
        )

    issue_words = _issue_signal_words(issue)
    stdout_words = _content_words(stdout)
    overlap = sorted(issue_words & stdout_words)

    # Bucket A: assertion failure (or "Issue reproduced") + ≥3 issue content words in stdout
    if _has_assertion_failure(stdout) and len(overlap) >= 3:
        return GatedTest(
            cand_id=candidate.cand_id,
            mask=candidate.mask,
            sample_idx=candidate.sample_idx,
            source=candidate.source,
            bucket="A",
            weight=1.0,
            buggy_exit=exit_code,
            buggy_stdout_tail=stdout[-500:],
            match_words=overlap[:10],
        )

    # Bucket B: specific exception matches issue.exception_type
    if issue.exception_type and _has_exception(stdout, issue.exception_type):
        return GatedTest(
            cand_id=candidate.cand_id,
            mask=candidate.mask,
            sample_idx=candidate.sample_idx,
            source=candidate.source,
            bucket="B",
            weight=0.7,
            buggy_exit=exit_code,
            buggy_stdout_tail=stdout[-500:],
        )

    # Bucket C: any non-zero / non-dropped error on buggy repo
    if exit_code != 0:
        return GatedTest(
            cand_id=candidate.cand_id,
            mask=candidate.mask,
            sample_idx=candidate.sample_idx,
            source=candidate.source,
            bucket="C",
            weight=0.4,
            buggy_exit=exit_code,
            buggy_stdout_tail=stdout[-500:],
        )

    # exit_code 0 but no "Issue resolved" marker OR unexpected signal — drop
    return GatedTest(
        cand_id=candidate.cand_id,
        mask=candidate.mask,
        sample_idx=candidate.sample_idx,
        source=candidate.source,
        bucket="DROP",
        weight=0.0,
        buggy_exit=exit_code,
        buggy_stdout_tail=stdout[-500:],
        drop_reason="no_failure_signal",
    )


def gate_candidates(
    instance: dict,
    candidates: list[Candidate],
    *,
    issue: IssueBundle,
    timeout: int = 60,
    cid: str | None = None,
) -> list[GatedTest]:
    """Run all candidates on the unpatched container, classify, drop junk.

    Opens one container per instance unless ``cid`` is provided.
    """
    scripts = {c.cand_id: c.source for c in candidates if c.parse_error is None and c.source.strip()}
    if not scripts:
        return []

    if cid is not None:
        outputs = run_scripts(cid, scripts, timeout=timeout)
    else:
        with docker_session(instance["instance_id"]) as c:
            if c is None:
                logger.warning("gate_candidates: container startup failed for %s", instance["instance_id"])
                return []
            outputs = run_scripts(c, scripts, timeout=timeout)

    by_id = {c.cand_id: c for c in candidates}
    gated: list[GatedTest] = []
    for cand_id, (exit_code, stdout) in outputs.items():
        candidate = by_id[cand_id]
        gated.append(classify_bucket(
            candidate=candidate,
            exit_code=exit_code,
            stdout=stdout,
            issue=issue,
        ))
    return gated

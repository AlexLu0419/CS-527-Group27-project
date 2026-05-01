"""Assemble judge input: issue text + stripped diff + ±N-line hunk context +
failing/passing test summaries."""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field

from unidiff import PatchSet

from sieve.judge.strip import strip_agent_narrative, strip_patch_metadata
from sieve.phases._docker import docker_exec_login, docker_session
from sieve.phases.reproduction import PerTestResult, ReproductionVerdict
from sieve.repro.cache import PhaseACache

logger = logging.getLogger("sieve.judge.assemble")

# Token-hygiene caps on judge prompt inputs. gpt-5-mini (400k ctx) can handle
# much larger, but trimming reduces cost + avoids pathological blow-ups on long
# issues. Existing local caps for traceback ([:1500]) and assertion ([:200])
# and passing-test one-liners ([:120]) are kept in their extraction helpers.
JUDGE_ISSUE_MAX_CHARS = 4000       # half of issue body cap (8192) for judge context
JUDGE_DIFF_MAX_CHARS = 6000        # matches feedback.py patch_diff[:6000]
JUDGE_TEST_SRC_MAX_CHARS = 3000    # per-test source (typical 60 lines * 50 chars)
JUDGE_MAX_HUNKS = 4                # cap hunk count; context_lines=30/hunk unchanged


@dataclass
class HunkContext:
    file_path: str
    pre_patch_lines: list[tuple[int, str]]   # (line_no, line)

    def to_dict(self) -> dict:
        return {"file_path": self.file_path, "pre_patch_lines": list(self.pre_patch_lines)}


@dataclass
class FailingTestInfo:
    cand_id: str
    source: str
    assertion: str | None
    minimal_traceback: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PassingTestInfo:
    cand_id: str
    name: str
    one_line_summary: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JudgeInput:
    instance_id: str
    issue_text: str
    unified_diff: str
    hunks: list[HunkContext] = field(default_factory=list)
    failing_tests: list[FailingTestInfo] = field(default_factory=list)
    passing_tests: list[PassingTestInfo] = field(default_factory=list)
    context_lines: int = 30
    zero_signal: bool = False  # true when no reproduction test survived admission

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "issue_text": self.issue_text,
            "unified_diff": self.unified_diff,
            "hunks": [h.to_dict() for h in self.hunks],
            "failing_tests": [t.to_dict() for t in self.failing_tests],
            "passing_tests": [t.to_dict() for t in self.passing_tests],
            "context_lines": self.context_lines,
            "zero_signal": self.zero_signal,
        }


_ASSERTION_RE = re.compile(r"^Assertion(?:Error)?[:\s](.+)$", re.MULTILINE)
_TRACEBACK_RE = re.compile(r"(Traceback \(most recent call last\):(?:\n.+)+?\n[A-Za-z_]+(?:Error|Exception|Warning)[^\n]*)", re.MULTILINE)


def _cat_range(cid: str, path: str, start: int, end: int) -> list[tuple[int, str]]:
    if end < start:
        return []
    cmd = f"sed -n '{start},{end}p' /testbed/{path}"
    r = docker_exec_login(cid, cmd, timeout=30)
    if r.returncode != 0:
        return []
    lines = r.stdout.splitlines()
    return [(start + i, line) for i, line in enumerate(lines)]


def _extract_hunks(diff: str, cid: str, *, context_lines: int) -> list[HunkContext]:
    try:
        ps = PatchSet(diff)
    except Exception as e:
        logger.warning("unidiff parse failed: %s", e)
        return []
    out: list[HunkContext] = []
    for patched in ps:
        path = patched.source_file or patched.target_file or ""
        path = path.lstrip("ab/").lstrip("/")
        if path.startswith("a/") or path.startswith("b/"):
            path = path[2:]
        if not path or path == "dev/null":
            continue
        for hunk in patched:
            start = max(1, hunk.source_start - context_lines)
            end = hunk.source_start + hunk.source_length + context_lines
            lines = _cat_range(cid, path, start, end)
            out.append(HunkContext(file_path=path, pre_patch_lines=lines))
    return out


def _extract_assertion(stdout: str) -> str | None:
    m = _ASSERTION_RE.search(stdout or "")
    if m:
        return m.group(1).strip()[:200]
    return None


def _extract_traceback(stdout: str) -> str | None:
    m = _TRACEBACK_RE.search(stdout or "")
    if m:
        return m.group(1)[:1500]
    return None


def _source_for(cache: PhaseACache, cand_id: str) -> str:
    for g in cache.gated_tests:
        if g.get("cand_id") == cand_id:
            return g.get("source", "")
    for c in cache.candidates:
        if c.get("cand_id") == cand_id:
            return c.get("source", "")
    return ""


def _clip(text: str, limit: int) -> str:
    """Tail-preserving clip — keep the head of the content (the first N chars).

    Issue bodies and diffs are most informative at the top (titles, first hunk);
    tests and hunks are likewise structured head-first. No truncation marker
    added because the judge prompt is already terse about "given this input".
    """
    if not text or len(text) <= limit:
        return text
    return text[:limit]


def assemble_judge_input(
    instance: dict,
    patch_content: str,
    repro: ReproductionVerdict,
    cache: PhaseACache,
    *,
    context_lines: int = 30,
) -> JudgeInput:
    issue_text = _clip(
        strip_agent_narrative(instance.get("problem_statement", "") or ""),
        JUDGE_ISSUE_MAX_CHARS,
    )
    diff = _clip(strip_patch_metadata(patch_content or ""), JUDGE_DIFF_MAX_CHARS)

    hunks: list[HunkContext] = []
    if diff.strip():
        with docker_session(instance["instance_id"]) as cid:
            if cid is not None:
                hunks = _extract_hunks(diff, cid, context_lines=context_lines)
    # Cap hunk count — large diffs can produce 10+ hunks; judge only needs a
    # representative sample to reason about patch shape.
    if len(hunks) > JUDGE_MAX_HUNKS:
        hunks = hunks[:JUDGE_MAX_HUNKS]

    failing: list[FailingTestInfo] = []
    passing: list[PassingTestInfo] = []
    for pt in repro.per_test:
        src = _source_for(cache, pt.cand_id)
        if pt.passed:
            # short summary only
            summary = (src.strip().splitlines()[0] if src.strip() else "")[:120]
            passing.append(PassingTestInfo(cand_id=pt.cand_id, name=pt.cand_id, one_line_summary=summary))
        else:
            failing.append(FailingTestInfo(
                cand_id=pt.cand_id,
                source=_clip(src, JUDGE_TEST_SRC_MAX_CHARS),
                assertion=_extract_assertion(pt.stdout_tail),
                minimal_traceback=_extract_traceback(pt.stdout_tail),
            ))

    return JudgeInput(
        instance_id=instance["instance_id"],
        issue_text=issue_text,
        unified_diff=diff,
        hunks=hunks,
        failing_tests=failing,
        passing_tests=passing,
        context_lines=context_lines,
        zero_signal=(repro.verdict == "UNCERTAIN_ZERO_SIGNAL"),
    )

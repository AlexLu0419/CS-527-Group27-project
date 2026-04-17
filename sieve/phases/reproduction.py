"""Layer 2b — Reproduction Test Check (Phase B).

Phase A (generate + gate) lives in ``sieve.repro.cache.phase_a``.
Phase B (this module) takes the cached gated tests, runs them against the
patched container, computes a weighted vote, and emits a verdict + feedback.

Thresholds (SIEVE v3 §4.2):
    S >= 0.70 → PASS
    S <= 0.40 → FAIL
    else      → UNCERTAIN

Candidate passes iff ``exit_code == 0`` OR stdout contains "Issue resolved"
(CodeMonkeys marker, per prompt pack §4).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from sieve.phases._docker import (
    apply_patch,
    copy_patch_to_container,
    docker_session,
)
from sieve.repro.cache import PhaseACache, phase_a
from sieve.repro.feedback import Feedback, synthesize_feedback
from sieve.repro.issue import IssueBundle
from sieve.repro.runner import run_scripts

logger = logging.getLogger("sieve.phases.reproduction")

PASS_THRESHOLD = 0.70
FAIL_THRESHOLD = 0.40


@dataclass
class PerTestResult:
    cand_id: str
    bucket: str
    weight: float
    passed: bool
    exit_code: int
    marker: str | None         # "Issue reproduced" | "Issue resolved" | "Other issues" | None
    stdout_tail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReproductionVerdict:
    verdict: str                              # PASS | FAIL | UNCERTAIN | UNCERTAIN_ZERO_SIGNAL | ERROR
    score: float = 0.0
    threshold_pass: float = PASS_THRESHOLD
    threshold_fail: float = FAIL_THRESHOLD
    per_test: list[PerTestResult] = field(default_factory=list)
    feedback: str | None = None
    feedback_detail: dict | None = None
    buckets_used: dict[str, int] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["per_test"] = [pt.to_dict() if hasattr(pt, "to_dict") else pt for pt in self.per_test]
        return d


def _detect_marker(stdout: str) -> str | None:
    if "Issue resolved" in stdout:
        return "Issue resolved"
    if "Issue reproduced" in stdout:
        return "Issue reproduced"
    if "Other issues" in stdout:
        return "Other issues"
    return None


def _did_pass(exit_code: int, stdout: str) -> bool:
    marker = _detect_marker(stdout)
    if marker == "Issue resolved":
        return True
    if marker == "Issue reproduced":
        return False
    return exit_code == 0


def _compute_score(results: list[PerTestResult]) -> float:
    total_w = sum(r.weight for r in results)
    if total_w <= 0:
        return 0.0
    hit = sum(r.weight for r in results if r.passed)
    return hit / total_w


def _buckets_used(results: list[PerTestResult]) -> dict[str, int]:
    buckets = {"A": 0, "B": 0, "C": 0}
    for r in results:
        if r.bucket in buckets:
            buckets[r.bucket] += 1
    return buckets


def _map_verdict(score: float) -> str:
    if score >= PASS_THRESHOLD:
        return "PASS"
    if score <= FAIL_THRESHOLD:
        return "FAIL"
    return "UNCERTAIN"


def phase_b(
    instance: dict,
    patch_content: str,
    cache: PhaseACache,
    *,
    timeout: int = 60,
    container_id: str | None = None,
) -> ReproductionVerdict:
    surviving = cache.surviving()
    issue = IssueBundle(**cache.issue)

    if cache.zero_signal or not surviving:
        return ReproductionVerdict(
            verdict="UNCERTAIN_ZERO_SIGNAL",
            score=0.0,
            per_test=[],
            message="no gated tests survived the buggy-repo gate",
            buckets_used={"A": 0, "B": 0, "C": 0},
        )

    scripts = {g.cand_id: g.source for g in surviving}
    by_id = {g.cand_id: g for g in surviving}

    if container_id is not None:
        outputs = _run_with_patched(container_id, patch_content, scripts, timeout=timeout)
    else:
        with docker_session(instance["instance_id"]) as cid:
            if cid is None:
                return ReproductionVerdict(
                    verdict="ERROR",
                    message="container_startup_failed",
                    buckets_used={"A": 0, "B": 0, "C": 0},
                )
            outputs = _run_with_patched(cid, patch_content, scripts, timeout=timeout)
            if outputs is None:
                return ReproductionVerdict(
                    verdict="ERROR",
                    message="patch_apply_or_copy_failed",
                    buckets_used={"A": 0, "B": 0, "C": 0},
                )

    if outputs is None:
        return ReproductionVerdict(
            verdict="ERROR",
            message="patch_apply_or_copy_failed",
            buckets_used={"A": 0, "B": 0, "C": 0},
        )

    per: list[PerTestResult] = []
    for cand_id, (exit_code, stdout) in outputs.items():
        g = by_id[cand_id]
        marker = _detect_marker(stdout or "")
        passed = _did_pass(exit_code, stdout or "")
        per.append(PerTestResult(
            cand_id=cand_id,
            bucket=g.bucket,
            weight=g.weight,
            passed=passed,
            exit_code=exit_code,
            marker=marker,
            stdout_tail=(stdout or "")[-500:],
        ))

    score = _compute_score(per)
    verdict_label = _map_verdict(score)
    verdict = ReproductionVerdict(
        verdict=verdict_label,
        score=score,
        per_test=per,
        buckets_used=_buckets_used(per),
        message=f"weighted_score={score:.3f} over {len(per)} tests",
    )

    if verdict_label == "FAIL":
        fb_obj = _synthesize_from_worst_failing(issue, patch_content, per)
        if fb_obj is not None:
            verdict.feedback = fb_obj.to_text() or None
            verdict.feedback_detail = fb_obj.to_dict()

    return verdict


def _synthesize_from_worst_failing(
    issue: IssueBundle,
    patch_content: str,
    per: list[PerTestResult],
) -> Feedback | None:
    failing = [r for r in per if not r.passed]
    if not failing:
        return None
    # Highest-weight failing test; break ties by bucket rank then cand_id.
    bucket_rank = {"A": 0, "B": 1, "C": 2}
    failing.sort(key=lambda r: (-r.weight, bucket_rank.get(r.bucket, 9), r.cand_id))
    target = failing[0]
    try:
        return synthesize_feedback(
            issue,
            patch_content,
            exit_code=target.exit_code,
            stdout=target.stdout_tail,
        )
    except Exception as e:
        logger.warning("feedback synth failed: %s", e)
        return None


def _run_with_patched(
    cid: str,
    patch_content: str,
    scripts: dict[str, str],
    *,
    timeout: int,
) -> dict[str, tuple[int, str]] | None:
    ok, err = copy_patch_to_container(cid, patch_content)
    if not ok:
        logger.warning("phase_b: copy patch failed — %s", err)
        return None
    ok, err = apply_patch(cid)
    if not ok:
        logger.warning("phase_b: apply patch failed — %s", err)
        return None
    return run_scripts(cid, scripts, timeout=timeout)


def run_reproduction_checks(
    instance: dict,
    patch_content: str,
    *,
    cache: PhaseACache | None = None,
    timeout: int = 60,
) -> ReproductionVerdict:
    """End-to-end Layer 2b: ensure Phase A is cached, then run Phase B."""
    cache = cache or phase_a(instance)
    return phase_b(instance, patch_content, cache, timeout=timeout)

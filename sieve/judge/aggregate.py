"""Lexicographic aggregation of reproduction + judge outputs into a FinalVerdict.

Rule (from SIEVE v3 §4.5):
- If repro is PASS or FAIL → passthrough; judge is never consulted.
- If repro is UNCERTAIN_ZERO_SIGNAL → stay UNCERTAIN; judge skipped.
- If repro is UNCERTAIN and judge is provided:
    * Upgrade to PASS iff judge.verdict==ACCEPT AND confidence==high AND
      weighted_score >= 0.75 AND rubric_scores.not_symptom_only == 1.
    * Downgrade to FAIL iff judge.verdict==REJECT OR
      rubric_scores.root_cause == 0 OR rubric_scores.not_symptom_only == 0.
    * Else stay UNCERTAIN (tie).
- If judge.parse_error is set, treat as tie (stay UNCERTAIN).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from sieve.phases.reproduction import ReproductionVerdict


@dataclass
class FinalVerdict:
    verdict: str             # PASS | FAIL | UNCERTAIN
    source: str              # repro | judge_upgrade | judge_downgrade | judge_tie | judge_skipped
    repro_verdict: str
    judge_verdict: str | None
    judge_confidence: str | None
    judge_weighted_score: float | None
    feedback: str | None
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def aggregate(repro: ReproductionVerdict, judge: "JudgeOutput | None") -> FinalVerdict:  # noqa: F821
    repro_v = repro.verdict
    base = FinalVerdict(
        verdict=repro_v,
        source="repro",
        repro_verdict=repro_v,
        judge_verdict=judge.verdict if judge else None,
        judge_confidence=judge.confidence if judge else None,
        judge_weighted_score=judge.weighted_score if judge else None,
        feedback=repro.feedback,
    )
    if repro_v in ("PASS", "FAIL"):
        return base
    if repro_v == "UNCERTAIN_ZERO_SIGNAL":
        # Keep it as UNCERTAIN for the cascade's purposes; judge skipped.
        base.verdict = "UNCERTAIN"
        base.source = "judge_skipped"
        base.message = "zero-signal; judge not consulted"
        return base
    if repro_v != "UNCERTAIN":
        # ERROR, etc.
        base.verdict = repro_v
        base.source = "repro"
        return base

    if judge is None or judge.parse_error:
        base.verdict = "UNCERTAIN"
        base.source = "judge_tie"
        base.message = "judge unavailable or parse error"
        return base

    rubric = judge.rubric_scores or {}
    not_symptom = rubric.get("not_symptom_only", 0)
    root_cause = rubric.get("root_cause", 0)

    # Upgrade
    if (
        judge.verdict == "ACCEPT"
        and judge.confidence == "high"
        and (judge.weighted_score or 0.0) >= 0.75
        and not_symptom == 1
        and root_cause == 1
    ):
        base.verdict = "PASS"
        base.source = "judge_upgrade"
        base.message = "judge upgraded uncertain to PASS"
        return base

    # Downgrade
    if judge.verdict == "REJECT" or root_cause == 0 or not_symptom == 0:
        base.verdict = "FAIL"
        base.source = "judge_downgrade"
        base.message = "judge downgraded uncertain to FAIL"
        return base

    base.verdict = "UNCERTAIN"
    base.source = "judge_tie"
    base.message = "judge did not break the tie"
    return base

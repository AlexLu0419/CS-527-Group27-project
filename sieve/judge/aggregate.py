"""Lexicographic aggregation of reproduction + judge outputs into a FinalVerdict.

Rule (SIEVE v3 §4.5, v1.1 upgrade tightening, v4 zero-signal routing,
v8 bucket-C loosening):
- If repro is PASS or FAIL → passthrough; judge is never consulted.
- If repro is UNCERTAIN or UNCERTAIN_ZERO_SIGNAL and judge is provided:
    * Upgrade to PASS iff judge.verdict==ACCEPT AND confidence==high AND
      rubric_scores.root_cause == 1 AND rubric_scores.not_symptom_only == 1
      AND rubric_scores.no_scope_creep == 1 AND reproduction produced no
      counted failing per-test in **bucket A or B** AND repro was NOT
      zero-signal. Bucket-C counted failures (generic errors on the
      buggy repo, weight 0.4) no longer block the upgrade: their low
      weight reflects weak signal, and vetoing clean judge upgrades on
      C-only noise over-indexed on bucket C by design. A/B counted
      failures still veto the upgrade.
    * Downgrade to FAIL iff judge.verdict==REJECT OR
      rubric_scores.root_cause == 0 OR rubric_scores.not_symptom_only == 0.
      (Downgrade is allowed on zero-signal — the judge can call a bad patch
      on patch+issue evidence alone, with the abstain-bias prompt guarding
      against over-commitment.)
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
    # UNCERTAIN and UNCERTAIN_ZERO_SIGNAL both route to the judge (v4).
    # The upgrade path is additionally blocked on zero-signal below.
    zero_signal = (repro_v == "UNCERTAIN_ZERO_SIGNAL")
    if repro_v not in ("UNCERTAIN", "UNCERTAIN_ZERO_SIGNAL"):
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
    no_scope_creep = rubric.get("no_scope_creep", 0)

    no_non_C_counted_failure = not any(
        getattr(pt, "counted", True)
        and not pt.passed
        and getattr(pt, "bucket", None) != "C"
        for pt in (repro.per_test or [])
    )

    # Upgrade: require the three patch-quality rubric items AND no counted
    # failing repro test in bucket A or B AND non-zero-signal reproduction.
    # The zero-signal block prevents PASS verdicts that stand on no verified
    # reproduction; the non-C-counted-failure block prevents overriding a
    # real fail-signal from the trustworthy buckets while allowing the judge
    # to override bucket-C noise (weight 0.4, generic errors).
    if (
        not zero_signal
        and judge.verdict == "ACCEPT"
        and judge.confidence == "high"
        and root_cause == 1
        and not_symptom == 1
        and no_scope_creep == 1
        and no_non_C_counted_failure
    ):
        base.verdict = "PASS"
        base.source = "judge_upgrade"
        base.message = "judge upgraded uncertain to PASS"
        return base

    # Downgrade: allowed on zero-signal. Judge's REJECT / rubric-zero is
    # trustworthy even without reproduction corroboration.
    if judge.verdict == "REJECT" or root_cause == 0 or not_symptom == 0:
        base.verdict = "FAIL"
        base.source = "judge_downgrade"
        base.message = (
            "judge downgraded uncertain to FAIL (zero-signal)"
            if zero_signal
            else "judge downgraded uncertain to FAIL"
        )
        return base

    base.verdict = "UNCERTAIN"
    base.source = "judge_tie"
    base.message = (
        "judge did not break the tie (zero-signal; upgrade blocked)"
        if zero_signal
        else "judge did not break the tie"
    )
    return base

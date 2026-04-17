"""Rule-based error bucketing for evolution cycle 1.

Reads ``sieve_v0_evolution_results.json`` (records written by ``run_sieve_v0.py``)
and emits one ``ErrorBucket`` per mislabeled or leaked-UNCERTAIN instance, tagged
with a ``responsible_layer`` and an ``artifact_edit_hint`` that points at the
single prompt / rubric item / threshold the human should revisit.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

ErrorKind = Literal["false_pass", "false_fail", "uncertain_leakage"]
ResponsibleLayer = Literal["static", "regression", "reproduction", "judge"]
EditHint = Literal[
    "mask_raw",
    "mask_traceback_first",
    "mask_snippet_first",
    "judge_rubric_item_1",  # root_cause
    "judge_rubric_item_2",  # not_symptom_only
    "judge_rubric_item_3",  # no_scope_creep
    "judge_rubric_item_4",  # failing_tests_legitimate
    "aggregation_threshold",
    "feedback_synth",
    "semgrep_rule",
    "static_check",
    "regression_gate",
]

# Walk order: cascade direction (shallow → deep).
CASCADE_ORDER: tuple[ResponsibleLayer, ...] = ("static", "regression", "reproduction", "judge")

PASS_LIKE = {"PASS", "ACCEPT"}
FAIL_LIKE = {"FAIL", "REJECT"}


@dataclass
class ErrorBucket:
    kind: ErrorKind
    responsible_layer: ResponsibleLayer
    artifact_edit_hint: EditHint
    instance_id: str
    ground_truth: str
    final_verdict: str
    final_source: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _layer_verdict(layers: dict, name: str) -> str | None:
    entry = layers.get(name)
    if not isinstance(entry, dict):
        return None
    return entry.get("verdict")


def _last_passing_layer(layers: dict) -> ResponsibleLayer:
    """Deepest layer that said PASS/ACCEPT in the cascade walk."""
    deepest: ResponsibleLayer = "static"
    for layer in CASCADE_ORDER:
        v = _layer_verdict(layers, layer)
        if v in PASS_LIKE:
            deepest = layer  # keep overwriting; last assignment wins.
    return deepest


def _first_failing_layer(layers: dict) -> ResponsibleLayer:
    """Shallowest layer that said FAIL/REJECT in the cascade walk."""
    for layer in CASCADE_ORDER:
        v = _layer_verdict(layers, layer)
        if v in FAIL_LIKE:
            return layer
    # Fallback: if nothing explicitly failed but the final verdict is FAIL,
    # point at whatever drove final_source.
    return "reproduction"


def _pick_false_pass_hint(responsible: ResponsibleLayer, layers: dict) -> EditHint:
    """Which artifact is most likely to blame when the cascade let a bad patch through?"""
    if responsible == "static":
        return "semgrep_rule"
    if responsible == "regression":
        return "regression_gate"
    if responsible == "reproduction":
        # The gated tests either didn't stress the real bug or the score threshold was too loose.
        repro = layers.get("reproduction") or {}
        buckets = repro.get("buckets_used") or {}
        if int(buckets.get("A", 0)) == 0:
            # No bucket-A test — likely the generator produced only symptom-level tests.
            return "mask_traceback_first"
        score = repro.get("score")
        if isinstance(score, (int, float)) and score < 0.85:
            return "aggregation_threshold"
        return "mask_raw"
    # judge
    judge = layers.get("judge") or {}
    rubric = judge.get("rubric_scores") or {}
    # The judge said ACCEPT when it shouldn't have — check which rubric item is most permissive.
    if rubric.get("root_cause", 1) == 1 and rubric.get("not_symptom_only", 1) == 1:
        # Judge confidently accepted; the rubric item that most commonly should have caught
        # a symptom-only patch is item 2.
        return "judge_rubric_item_2"
    if rubric.get("no_scope_creep", 1) == 1:
        return "judge_rubric_item_3"
    return "judge_rubric_item_1"


def _pick_false_fail_hint(responsible: ResponsibleLayer, layers: dict) -> EditHint:
    """Which artifact is most likely to blame when the cascade rejected a good patch?"""
    if responsible == "static":
        return "semgrep_rule"
    if responsible == "regression":
        return "regression_gate"
    if responsible == "reproduction":
        repro = layers.get("reproduction") or {}
        score = repro.get("score")
        if isinstance(score, (int, float)) and score > 0.25:
            # Close-call false-fail: loosening the threshold would fix it.
            return "aggregation_threshold"
        buckets = repro.get("buckets_used") or {}
        if int(buckets.get("A", 0)) == 0 and int(buckets.get("C", 0)) > 0:
            # Only noisy bucket-C tests — likely Mask 1 over-constrained.
            return "mask_raw"
        return "mask_snippet_first"
    # judge
    judge = layers.get("judge") or {}
    rubric = judge.get("rubric_scores") or {}
    # Judge rejected a patch that actually worked — find the zero.
    for key, hint in (
        ("failing_tests_legitimate", "judge_rubric_item_4"),
        ("no_scope_creep",           "judge_rubric_item_3"),
        ("not_symptom_only",         "judge_rubric_item_2"),
        ("root_cause",               "judge_rubric_item_1"),
    ):
        if rubric.get(key, 1) == 0:
            return hint  # type: ignore[return-value]
    return "judge_rubric_item_1"


def _pick_uncertain_hint(layers: dict) -> tuple[ResponsibleLayer, EditHint]:
    """UNCERTAIN-on-unresolved = leaked bug. Blame the layer best positioned to catch it."""
    repro = layers.get("reproduction") or {}
    buckets = repro.get("buckets_used") or {}
    a_count = int(buckets.get("A", 0))

    judge = layers.get("judge")
    if isinstance(judge, dict) and judge.get("verdict"):
        # Judge ran but gave UNCERTAIN / weak ACCEPT. Item 4 is the typical culprit —
        # the judge couldn't decide whether the failing repro tests were legitimate.
        rubric = judge.get("rubric_scores") or {}
        if rubric.get("failing_tests_legitimate", 1) == 1 and a_count == 0:
            return "reproduction", "mask_traceback_first"
        return "judge", "judge_rubric_item_4"

    # Judge skipped (zero-signal or skipped path).
    if a_count == 0:
        return "reproduction", "mask_traceback_first"
    # Bucket-A exists but vote wasn't decisive — threshold is the lever.
    return "reproduction", "aggregation_threshold"


def _evidence(rec: dict) -> dict:
    layers = rec.get("layers", {})
    ev = {
        "final_verdict": rec.get("final_verdict"),
        "final_source": rec.get("final_source"),
        "static": (layers.get("static") or {}).get("verdict"),
        "regression": (layers.get("regression") or {}).get("verdict"),
        "reproduction": {
            k: (layers.get("reproduction") or {}).get(k)
            for k in ("verdict", "score", "buckets_used", "feedback", "message")
            if (layers.get("reproduction") or {}).get(k) is not None
        },
    }
    judge = layers.get("judge")
    if isinstance(judge, dict):
        ev["judge"] = {
            k: judge.get(k)
            for k in ("verdict", "confidence", "weighted_score", "rubric_scores", "parse_error")
            if judge.get(k) is not None
        }
    return ev


def classify_errors(results: list[dict]) -> list[ErrorBucket]:
    buckets: list[ErrorBucket] = []
    for rec in results:
        gt = rec.get("ground_truth")
        fv = rec.get("final_verdict")
        if gt not in ("resolved", "unresolved"):
            continue
        if fv is None:
            continue
        layers = rec.get("layers", {})
        common = dict(
            instance_id=rec["instance_id"],
            ground_truth=gt,
            final_verdict=fv,
            final_source=rec.get("final_source", ""),
            evidence=_evidence(rec),
        )

        if fv == "PASS" and gt == "unresolved":
            responsible = _last_passing_layer(layers)
            hint = _pick_false_pass_hint(responsible, layers)
            buckets.append(ErrorBucket(
                kind="false_pass",
                responsible_layer=responsible,
                artifact_edit_hint=hint,
                **common,
            ))
        elif fv == "FAIL" and gt == "resolved":
            responsible = _first_failing_layer(layers)
            hint = _pick_false_fail_hint(responsible, layers)
            buckets.append(ErrorBucket(
                kind="false_fail",
                responsible_layer=responsible,
                artifact_edit_hint=hint,
                **common,
            ))
        elif fv == "UNCERTAIN" and gt == "unresolved":
            responsible, hint = _pick_uncertain_hint(layers)
            buckets.append(ErrorBucket(
                kind="uncertain_leakage",
                responsible_layer=responsible,
                artifact_edit_hint=hint,
                **common,
            ))
        # PASS+resolved (TN), FAIL+unresolved (TP), UNCERTAIN+resolved (tolerated): no bucket.
    return buckets


def load_results(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def summarize(buckets: list[ErrorBucket]) -> dict:
    by_kind: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    by_hint: dict[str, int] = {}
    for b in buckets:
        by_kind[b.kind] = by_kind.get(b.kind, 0) + 1
        by_layer[b.responsible_layer] = by_layer.get(b.responsible_layer, 0) + 1
        by_hint[b.artifact_edit_hint] = by_hint.get(b.artifact_edit_hint, 0) + 1
    return {
        "total_errors": len(buckets),
        "by_kind": by_kind,
        "by_responsible_layer": by_layer,
        "by_artifact_edit_hint": by_hint,
    }

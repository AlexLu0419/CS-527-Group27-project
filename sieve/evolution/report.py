"""Render ``report_cycle1.md`` — the human-edit worksheet for a self-evolution cycle.

Groups error buckets by ``artifact_edit_hint`` so the reviewer can edit one prompt
(or threshold) at a time, with the concrete failing instances listed in-line.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from sieve.evolution.classify import ErrorBucket, summarize
from sieve.prompts import PROMPTS_DIR, load_prompt

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Human-readable description per artifact edit hint — shown once per section header.
HINT_DESCRIPTIONS: dict[str, str] = {
    "mask_raw":               "Mask 1 (raw issue body → reproduction test).",
    "mask_traceback_first":   "Mask 2 (traceback-first reproduction test).",
    "mask_snippet_first":     "Mask 3 (reporter-snippet-first reproduction test).",
    "judge_rubric_item_1":    "Judge rubric item 1: root_cause.",
    "judge_rubric_item_2":    "Judge rubric item 2: not_symptom_only.",
    "judge_rubric_item_3":    "Judge rubric item 3: no_scope_creep.",
    "judge_rubric_item_4":    "Judge rubric item 4: failing_tests_legitimate.",
    "aggregation_threshold":  "Reproduction PASS/FAIL score thresholds (0.70 / 0.40).",
    "feedback_synth":         "Feedback synthesizer prompt.",
    "semgrep_rule":           "Static-analysis Semgrep ruleset.",
    "static_check":           "Layer-1 static checks (patch apply / parse / lint-delta).",
    "regression_gate":        "Layer-2a regression PASS_TO_PASS gate.",
}

HINT_TO_PROMPT: dict[str, str] = {
    "mask_raw":             "mask_raw",
    "mask_traceback_first": "mask_traceback_first",
    "mask_snippet_first":   "mask_snippet_first",
    "judge_rubric_item_1":  "judge_patch",
    "judge_rubric_item_2":  "judge_patch",
    "judge_rubric_item_3":  "judge_patch",
    "judge_rubric_item_4":  "judge_patch",
    "feedback_synth":       "feedback_synth",
}


def _yaml_excerpt(prompt_name: str, *, max_lines: int = 60) -> str:
    path = PROMPTS_DIR / f"{prompt_name}.yaml"
    if not path.exists():
        return f"  (prompt file missing: {path.name})"
    text = path.read_text()
    lines = text.splitlines()
    if len(lines) <= max_lines:
        body = "\n".join(lines)
    else:
        body = "\n".join(lines[:max_lines]) + f"\n# … ({len(lines) - max_lines} more lines — edit the file directly)"
    return body


def _format_evidence(ev: dict) -> str:
    out = ["  | Layer | Verdict / detail |", "  |---|---|"]
    out.append(f"  | static | `{ev.get('static')}` |")
    out.append(f"  | regression | `{ev.get('regression')}` |")
    repro = ev.get("reproduction") or {}
    if repro:
        rline = f"verdict=`{repro.get('verdict')}`"
        if "score" in repro:
            rline += f", score=`{repro['score']:.3f}`" if isinstance(repro["score"], (int, float)) else f", score=`{repro['score']}`"
        if "buckets_used" in repro:
            rline += f", buckets={repro['buckets_used']}"
        out.append(f"  | reproduction | {rline} |")
        if repro.get("feedback"):
            fb = str(repro["feedback"]).strip().replace("\n", " ")
            if len(fb) > 300:
                fb = fb[:297] + "…"
            out.append(f"  | reproduction.feedback | {fb} |")
    judge = ev.get("judge")
    if judge:
        jline = f"verdict=`{judge.get('verdict')}`, conf=`{judge.get('confidence')}`, S=`{judge.get('weighted_score')}`"
        if "rubric_scores" in judge:
            jline += f", rubric={judge['rubric_scores']}"
        out.append(f"  | judge | {jline} |")
    out.append(f"  | final | `{ev.get('final_verdict')}` (via `{ev.get('final_source')}`) |")
    return "\n".join(out)


def _bucket_section(hint: str, buckets: list[ErrorBucket]) -> str:
    lines: list[str] = []
    lines.append(f"## Hint: `{hint}` — {len(buckets)} instance{'s' if len(buckets) != 1 else ''}")
    lines.append("")
    lines.append(f"> {HINT_DESCRIPTIONS.get(hint, 'Artifact target for this edit.')}")
    lines.append("")

    # If we can point at a concrete YAML file, include an excerpt and the edit path.
    prompt_name = HINT_TO_PROMPT.get(hint)
    if prompt_name:
        try:
            spec = load_prompt(prompt_name)
            lines.append(f"**File to edit:** `sieve/prompts/{prompt_name}.yaml` "
                         f"(current version: `{spec.version}`, hash: `{spec.content_hash()[:20]}…`)")
        except Exception:
            lines.append(f"**File to edit:** `sieve/prompts/{prompt_name}.yaml`")
        lines.append("")
        lines.append("<details><summary>Current prompt excerpt (click to expand)</summary>")
        lines.append("")
        lines.append("```yaml")
        lines.append(_yaml_excerpt(prompt_name))
        lines.append("```")
        lines.append("")
        lines.append("</details>")
    elif hint == "aggregation_threshold":
        lines.append("**Parameter to adjust:** `PASS_THRESHOLD` / `FAIL_THRESHOLD` in `sieve/phases/reproduction.py` (currently `0.70` / `0.40`).")
    elif hint in ("semgrep_rule", "static_check"):
        lines.append("**Target:** `sieve/phases/static.py` + associated Semgrep rules.")
    elif hint == "regression_gate":
        lines.append("**Target:** regression gate logic in `sieve/phases/dynamic.py::check_regression_tests`.")
    lines.append("")

    lines.append("### Affected instances")
    lines.append("")
    for b in sorted(buckets, key=lambda x: x.instance_id):
        kind_badge = {
            "false_pass":         "FALSE_PASS",
            "false_fail":         "FALSE_FAIL",
            "uncertain_leakage":  "UNCERTAIN_LEAK",
        }.get(b.kind, b.kind.upper())
        lines.append(f"#### `{b.instance_id}` — {kind_badge} (gt=`{b.ground_truth}`, responsible=`{b.responsible_layer}`)")
        lines.append("")
        lines.append(_format_evidence(b.evidence))
        lines.append("")

    lines.append("### Suggested edit direction")
    lines.append("")
    lines.append(_edit_suggestion(hint, buckets))
    lines.append("")
    return "\n".join(lines)


def _edit_suggestion(hint: str, buckets: list[ErrorBucket]) -> str:
    kinds = {b.kind for b in buckets}
    notes: list[str] = []
    if hint == "mask_traceback_first":
        notes.append(
            "- Inspect failing instances where `buckets_used.A == 0`. If the generator keeps producing "
            "symptom-level tests (e.g. only checking return type instead of value), tighten the mask prompt "
            "to require an exact exception match *and* an assertion on the post-condition described in `expected_behavior`."
        )
        if "false_pass" in kinds:
            notes.append(
                "- At least one false-PASS has no bucket-A survivor. Consider adding an example in the mask "
                "prompt that demonstrates the difference between reproducing the *crash* vs reproducing the *buggy value*."
            )
    elif hint == "mask_raw":
        notes.append(
            "- Inspect the generated Mask-1 test scripts listed above. If they over-constrain "
            "(e.g. hard-code implementation details), relax the instructions to focus on observable behavior."
        )
    elif hint == "mask_snippet_first":
        notes.append(
            "- The reporter snippet was used but the resulting test didn't correlate with the true bug. "
            "Consider adding explicit guidance in the mask prompt to adapt — not copy — the snippet."
        )
    elif hint == "judge_rubric_item_1":
        notes.append(
            "- The judge marked `root_cause=0` on patches that do fix the root cause, or `=1` on patches that only shuffle symptoms. "
            "Sharpen the item's phrasing in `judge_patch.yaml` with a verb-first decision criterion."
        )
    elif hint == "judge_rubric_item_2":
        notes.append(
            "- `not_symptom_only` is the lever for catching patches that mask the bug rather than fix it. "
            "Add a concrete counter-example to the judge prompt — e.g., 'returning None on error instead of raising is symptom-only.'"
        )
    elif hint == "judge_rubric_item_3":
        notes.append(
            "- `no_scope_creep` should flag patches that touch unrelated files or add unrequested features. "
            "Tighten the criterion in `judge_patch.yaml` to include a file-count heuristic."
        )
    elif hint == "judge_rubric_item_4":
        notes.append(
            "- `failing_tests_legitimate` is the most common source of UNCERTAIN leakage. "
            "Clarify what 'legitimate' means: does the test exercise the issue's expected behavior, or is it too narrow?"
        )
    elif hint == "aggregation_threshold":
        notes.append(
            "- Close-call verdicts dominate this group. Run a threshold sweep (`0.65 / 0.45`, `0.75 / 0.35`) on the "
            "evolution slice and pick the pair that minimises (FP + FN) without increasing UNCERTAIN."
        )
    elif hint == "feedback_synth":
        notes.append(
            "- Feedback strings were vague or echoed test code. Tighten the synthesizer prompt to require "
            "(scenario, conceptual_gap, direction) triples with one sentence each."
        )
    elif hint in ("semgrep_rule", "static_check", "regression_gate"):
        notes.append("- Review the rule/gate logic; self-evolution does not auto-modify non-prompt code.")
    if not notes:
        notes.append("- No pre-baked suggestion — human review required.")
    return "\n".join(notes)


def build_report(
    buckets: list[ErrorBucket],
    manifest: dict | None,
    *,
    out_path: Path,
    cycle: int = 1,
) -> Path:
    summary = summarize(buckets)
    lines: list[str] = []
    lines.append(f"# SIEVE evolution — cycle {cycle} report")
    lines.append("")
    if manifest is not None:
        lines.append(f"- Slice seed: `{manifest.get('seed')}`")
        lines.append(f"- Evolution slice size: `{manifest.get('evolution_size')}`")
        lines.append(f"- Held-out size: `{manifest.get('holdout_size')}`")
        lines.append(f"- Dropped errors: `{len(manifest.get('dropped_error_ids', []))}`")
    lines.append(f"- Total error buckets: `{summary['total_errors']}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("### By kind")
    for k, v in sorted(summary["by_kind"].items()):
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    lines.append("### By responsible layer")
    for k, v in sorted(summary["by_responsible_layer"].items()):
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    lines.append("### By artifact edit hint")
    for k, v in sorted(summary["by_artifact_edit_hint"].items()):
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Group buckets by hint and order by size (largest first).
    by_hint: dict[str, list[ErrorBucket]] = {}
    for b in buckets:
        by_hint.setdefault(b.artifact_edit_hint, []).append(b)
    ordered_hints = sorted(by_hint.keys(), key=lambda h: (-len(by_hint[h]), h))

    for hint in ordered_hints:
        lines.append(_bucket_section(hint, by_hint[hint]))
        lines.append("---")
        lines.append("")

    lines.append("## Acceptance workflow")
    lines.append("")
    lines.append("1. Edit one YAML (or threshold) at a time.")
    lines.append("2. Bump the `version:` field of any edited prompt.")
    lines.append("3. Regenerate `sieve/prompts/ARTIFACT_VERSIONS.json` (run `python -c 'from sieve.evolution.versions import current_versions; import json; print(json.dumps(current_versions(), indent=2))' > sieve/prompts/ARTIFACT_VERSIONS.json`).")
    lines.append("4. Run `python scripts/validate_evolution_delta.py --baseline HEAD~1 --candidate HEAD --split held_out`.")
    lines.append("5. Accept iff held-out accuracy strictly improves AND evolution-slice accuracy does not drop by more than one instance.")
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))

    # Also write a machine-readable sibling for downstream consumers.
    sibling = out_path.with_suffix(".json")
    sibling.write_text(json.dumps({
        "summary": summary,
        "buckets": [asdict(b) for b in buckets],
    }, indent=2))
    return out_path

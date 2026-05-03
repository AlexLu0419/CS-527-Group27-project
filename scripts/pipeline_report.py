#!/usr/bin/env python3
"""pipeline_report.py — assemble cost + layer-passthrough report after a
full SIEVE pipeline run (Phase A + reproduction + manifest + retry + harness).

Both cost numbers are EXACT (no estimation):

- SIEVE LLM calls (Phase A localizer/generator/judge/feedback): each
  ``runs/llm_log/{role}.jsonl`` entry has
  ``usage.{prompt_tokens, completion_tokens}`` → exact tokens × per-1M rates.
- Mini-SWE-agent retry: each ``<iid>.traj.json`` has
  ``info.model_stats.instance_cost`` → exact provider charge.

Reads:
- runs/llm_log/{localize,generate,judge,feedback}.jsonl  → token usage per role
- runs/static_checks_validation_gpt5mini/results.json    → static layer outcomes
- runs/dynamic_regression_validation_gpt5mini/results.json → dynreg outcomes
- runs/reproduction_validation_gpt5mini/results.json     → reproduction outcomes
- runs/judge_validation_gpt5mini/results.json            → judge outcomes
- <retry-dir>/feedback_manifest.json                     → retry roster size
- <retry-preds-dir>/preds.json                           → retry preds
- <retry-preds-dir>/<iid>/<iid>.traj.json                → per-instance cost
- <harness-report>                                       → harness verdict
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Per-million-token prices (USD).
PRICES = {
    "gemini/gemini-2.5-pro":   {"in": 1.25,  "out": 10.0},
    "gemini/gemini-2.5-flash": {"in": 0.30,  "out": 2.50},
    "openai/gpt-5-mini":       {"in": 0.25,  "out": 2.00},
}


def _parse_iso(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _tally_role(jsonl_path: Path, since_ts: float) -> dict:
    """Sum prompt/completion tokens per model for successful entries since
    the given timestamp. Returns {model: {requests, prompt_tokens, completion_tokens}}.
    """
    bucket: dict[str, dict] = defaultdict(lambda: {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
    if not jsonl_path.exists():
        return {}
    with jsonl_path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("ts", 0) < since_ts:
                continue
            if d.get("error"):
                continue  # error entries don't have usage
            usage = d.get("usage") or {}
            if not usage:
                continue
            model = d.get("model", "unknown")
            b = bucket[model]
            b["requests"] += 1
            b["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            b["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    return dict(bucket)


def _cost_of(bucket_by_model: dict) -> tuple[float, dict]:
    """Apply per-1M pricing. Returns (total_cost, per-model breakdown)."""
    total = 0.0
    out = {}
    for model, b in bucket_by_model.items():
        p = PRICES.get(model)
        if p is None:
            out[model] = {**b, "cost_usd": None, "note": "unknown model — no price"}
            continue
        cost = (b["prompt_tokens"] / 1e6) * p["in"] + (b["completion_tokens"] / 1e6) * p["out"]
        total += cost
        out[model] = {**b, "cost_usd": round(cost, 4)}
    return round(total, 4), out


def _retry_cost_from_trajectories(preds_dir: Path) -> tuple[float, int, dict]:
    """Sum mini-swe-agent's per-instance cost. Returns (total, n_instances, per_iid)."""
    total = 0.0
    per_iid: dict[str, dict] = {}
    if not preds_dir.exists():
        return 0.0, 0, {}
    for traj in sorted(preds_dir.glob("*/*.traj.json")):
        try:
            d = json.loads(traj.read_text())
            ms = (d.get("info") or {}).get("model_stats") or {}
            cost = float(ms.get("instance_cost") or 0)
            calls = int(ms.get("api_calls") or 0)
            iid = d.get("instance_id") or traj.parent.name
            per_iid[iid] = {"cost_usd": round(cost, 4), "api_calls": calls}
            total += cost
        except Exception:
            continue
    return round(total, 4), len(per_iid), per_iid


def _layer_outcomes(results_path: Path, key: str = "verdict") -> Counter:
    if not results_path.exists():
        return Counter()
    data = json.loads(results_path.read_text())
    if isinstance(data, dict):
        data = list(data.values())
    def _val(r):
        v = r.get(key)
        if isinstance(v, dict):  # e.g. judge.final is a dict carrying {'verdict': ...}
            v = v.get("verdict", "?")
        return v if v is not None else "?"
    return Counter(_val(r) for r in data if isinstance(r, dict))


def _harness_outcomes(report_path: Path) -> dict | None:
    if not report_path.exists():
        return None
    d = json.loads(report_path.read_text())
    return {
        "resolved":     d.get("resolved_ids", []),
        "unresolved":   d.get("unresolved_ids", []),
        "errors":       d.get("error_ids", []),
        "empty":        d.get("empty_patch_ids", []),
        "completed":    d.get("completed_ids", []),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", required=True,
                    help="ISO timestamp marking pipeline start, e.g. 2026-04-25T23:38:00")
    ap.add_argument("--retry-dir", type=Path,
                    default=REPO_ROOT / "runs/retry_gpt5mini",
                    help="Retry manifest + run.log dir")
    ap.add_argument("--retry-preds-dir", type=Path,
                    default=REPO_ROOT / "runs/retry_gpt5mini_preds",
                    help="Retry preds dir (with per-instance traj.json files)")
    ap.add_argument("--repro-results", type=Path,
                    default=REPO_ROOT / "runs/reproduction_validation_gpt5mini/results.json",
                    help="Reproduction layer results.json")
    ap.add_argument("--harness-report", type=Path, required=True,
                    help="Harness report.json")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs/pipeline_report.md")
    args = ap.parse_args()

    since_ts = _parse_iso(args.since)

    # --- LLM costs (exact, from token usage) ---
    localize_b = _tally_role(REPO_ROOT / "runs/llm_log/localize.jsonl", since_ts)
    generate_b = _tally_role(REPO_ROOT / "runs/llm_log/generate.jsonl", since_ts)
    judge_b    = _tally_role(REPO_ROOT / "runs/llm_log/judge.jsonl",    since_ts)
    feedback_b = _tally_role(REPO_ROOT / "runs/llm_log/feedback.jsonl", since_ts)

    localize_cost, localize_break = _cost_of(localize_b)
    generate_cost, generate_break = _cost_of(generate_b)
    judge_cost,    judge_break    = _cost_of(judge_b)
    feedback_cost, feedback_break = _cost_of(feedback_b)

    # --- Retry agent cost (exact, from trajectory model_stats) ---
    retry_cost, retry_n_traj, retry_per_iid = _retry_cost_from_trajectories(args.retry_preds_dir)

    # --- Layer pass-through ---
    # Static layer has no single `verdict` — it has three sub-checks.
    # Tally per-check pass-rates instead.
    static_path = REPO_ROOT / "runs/static_checks_validation_gpt5mini/results.json"
    static_outcomes: dict = {}
    if static_path.exists():
        static_data = json.loads(static_path.read_text())
        if isinstance(static_data, dict):
            static_data = list(static_data.values())
        from collections import defaultdict as _dd
        per_check: dict = _dd(Counter)
        for r in static_data:
            for k in ("check_patch_applies",):
                sub = r.get(k) or {}
                v = sub.get("verdict") if isinstance(sub, dict) else sub
                per_check[k][v or "MISSING"] += 1
        static_outcomes = {k: dict(v) for k, v in per_check.items()}

    dynreg_outcomes  = _layer_outcomes(REPO_ROOT / "runs/dynamic_regression_validation_gpt5mini/results.json")
    repro_outcomes   = _layer_outcomes(args.repro_results)
    judge_outcomes   = _layer_outcomes(REPO_ROOT / "runs/judge_validation_gpt5mini/results.json", key="final")

    manifest_path = args.retry_dir / "feedback_manifest.json"
    roster_n = len(json.loads(manifest_path.read_text())) if manifest_path.exists() else None

    retry_preds_path = args.retry_preds_dir / "preds.json"
    retry_n_total = retry_n_nonempty = None
    if retry_preds_path.exists():
        rp = json.loads(retry_preds_path.read_text())
        retry_n_total = len(rp)
        retry_n_nonempty = sum(1 for v in rp.values() if (v.get("model_patch") or "").strip())

    harness = _harness_outcomes(args.harness_report)

    # --- Render report ---
    L: list[str] = []
    L.append("# pipeline report")
    L.append("")
    L.append(f"Pipeline window starts: `{args.since}`  (since_ts={since_ts:.0f})")
    L.append("")
    L.append("All LLM costs are computed from logged token counts × published per-1M-token prices, not estimates. Mini-SWE-agent costs come from each trajectory's `info.model_stats.instance_cost` (exact).")
    L.append("")

    L.append("## Cost summary")
    L.append("")
    L.append("| Step | Service / Model | Requests | Prompt tokens | Completion tokens | Cost (USD) |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    def _row(step, breakdown):
        for model, b in breakdown.items():
            L.append(
                f"| {step} | {model} | {b['requests']} | {b['prompt_tokens']:,} | {b['completion_tokens']:,} | "
                f"${b.get('cost_usd', 0):.4f} |"
            )
    if localize_break:
        _row("Phase A localize", localize_break)
    if generate_break:
        _row("Phase A generate", generate_break)
    if judge_break:
        _row("Cascade judge", judge_break)
    if feedback_break:
        _row("Repro layer feedback", feedback_break)
    L.append(
        f"| Mini-SWE-agent retry | openai/gpt-5-mini | {retry_n_traj} traj | (token detail in traj.json) | (token detail in traj.json) | ${retry_cost:.4f} |"
    )
    L.append("| Focal-snippet extraction | (Docker only, no LLM) | — | — | — | $0.0000 |")
    L.append("| Reproduction layer (Phase B) | (Docker only, no LLM) | — | — | — | $0.0000 |")
    L.append("| SWE-bench harness | (Docker only, no LLM) | — | — | — | $0.0000 |")
    total = localize_cost + generate_cost + judge_cost + feedback_cost + retry_cost
    L.append("")
    L.append(f"**Total LLM cost: ${total:.2f}**  (Docker compute excluded.)")
    L.append("")

    L.append("## Layer pass-through (50 instances total)")
    L.append("")
    L.append("Per-layer verdict counts (cascade reads these in order static → regression → reproduction → judge):")
    L.append("")
    def _outcomes_block(name, c):
        L.append(f"**{name}:**")
        if not c:
            L.append("- (no results.json found)")
        for k, v in sorted(c.items()):
            L.append(f"- {k}: {v}")
        L.append("")
    # Static — render per-sub-check
    L.append("**Static check (git apply --check):**")
    if not static_outcomes:
        L.append("- (no results.json found)")
    for check, dist in static_outcomes.items():
        parts = ", ".join(f"{k}={v}" for k, v in sorted(dist.items()))
        L.append(f"- {check}: {parts}")
    L.append("")
    _outcomes_block("Dynamic regression check (sieve.phases.dynamic_regression)", dynreg_outcomes)
    _outcomes_block("Reproduction check (Phase A → Phase B → cascade verdict)", repro_outcomes)
    _outcomes_block("Judge layer (tiebreak on UNCERTAIN)", judge_outcomes)

    L.append(f"**Retry roster size:** {roster_n}  (FAIL ∪ UNCERTAIN after cascade)")
    if retry_n_total is not None:
        L.append(f"**Retry preds:** {retry_n_total} entries, {retry_n_nonempty} non-empty patches")
    L.append("")

    L.append("## Final harness")
    L.append("")
    if harness:
        n_resolved = len(harness["resolved"])
        L.append(f"- Resolved: **{n_resolved}** / 50")
        L.append(f"- Unresolved: {len(harness['unresolved'])}")
        L.append(f"- Errors: {len(harness['errors'])}")
        L.append(f"- Empty patches: {len(harness['empty'])}")
        L.append(f"- Completed: {len(harness['completed'])}")
        if harness['errors']:
            L.append(f"- Error instances: {sorted(harness['errors'])}")
    else:
        L.append("(harness report not found yet)")
    L.append("")

    if retry_per_iid:
        L.append("## Per-instance retry cost (top spenders)")
        L.append("")
        L.append("| Instance | Cost (USD) | API calls |")
        L.append("| --- | --- | --- |")
        ranked = sorted(retry_per_iid.items(), key=lambda kv: -kv[1]['cost_usd'])
        for iid, stats in ranked[:10]:
            L.append(f"| {iid} | ${stats['cost_usd']:.4f} | {stats['api_calls']} |")
        L.append("")

    text = "\n".join(L)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text)
    print()
    print(f"Written to: {args.out}")


if __name__ == "__main__":
    main()

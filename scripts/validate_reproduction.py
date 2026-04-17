#!/usr/bin/env python3
"""validate_reproduction.py

Validates Layer-2b reproduction (Phase A generate+gate → Phase B weighted vote)
from ``sieve.phases.reproduction`` on the 50-instance Gemini 2.5 Pro pilot set.

Outputs
-------
  runs/reproduction_validation/results.json
  runs/reproduction_validation/summary.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PREDS_JSON = (
    REPO_ROOT
    / "runs/swe-verified_50_gemini-2.5-pro"
    / "swe_verified_50_gemini-2.5-pro-new"
    / "preds.json"
)
GT_JSON = (
    REPO_ROOT
    / "runs/sb-cli-reports"
    / "gemini__gemini-2.5-pro.gemini-2.5-pro-mini50-run.json"
)
OUTPUT_DIR = REPO_ROOT / "runs/reproduction_validation"
OUTPUT_JSON = OUTPUT_DIR / "results.json"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"

from sieve.phases.reproduction import run_reproduction_checks
from sieve.repro.cache import phase_a
from sieve.utils.swebench import load_instances


def process_instance(instance: dict, patch_content: str, gt_label: str, *, timeout: int) -> dict:
    iid = instance["instance_id"]
    if not patch_content.strip():
        return {
            "instance_id": iid,
            "ground_truth": gt_label,
            "verdict": "SKIP",
            "score": 0.0,
            "message": "empty patch",
            "buckets_used": {},
            "n_surviving": 0,
        }
    try:
        cache = phase_a(instance)
    except Exception as exc:
        print(f"  [PHASE_A EXCEPTION] {exc}")
        return {
            "instance_id": iid,
            "ground_truth": gt_label,
            "verdict": "ERROR",
            "score": 0.0,
            "message": f"phase_a: {exc}",
            "buckets_used": {},
            "n_surviving": 0,
        }

    surviving = cache.surviving()
    n_surviving = len(surviving)
    print(f"  Phase A: {n_surviving} surviving gated tests (zero_signal={cache.zero_signal})")

    try:
        verdict = run_reproduction_checks(instance, patch_content, cache=cache, timeout=timeout)
    except Exception as exc:
        print(f"  [PHASE_B EXCEPTION] {exc}")
        return {
            "instance_id": iid,
            "ground_truth": gt_label,
            "verdict": "ERROR",
            "score": 0.0,
            "message": f"phase_b: {exc}",
            "buckets_used": {},
            "n_surviving": n_surviving,
        }

    row = {
        "instance_id": iid,
        "ground_truth": gt_label,
        "verdict": verdict.verdict,
        "score": verdict.score,
        "message": verdict.message,
        "buckets_used": verdict.buckets_used,
        "n_surviving": n_surviving,
        "feedback": verdict.feedback,
        "per_test": [pt.to_dict() for pt in verdict.per_test],
    }
    print(f"  {verdict.verdict}  S={verdict.score:.2f}  buckets={verdict.buckets_used}")
    return row


def confusion_stats(results: list[dict]) -> dict:
    """Confusion with PASS/FAIL; UNCERTAIN* counted separately."""
    tp = fp = tn = fn = unc = zs = err = skip = 0
    for r in results:
        v = r["verdict"]
        gt = r["ground_truth"]
        if v == "SKIP":
            skip += 1
            continue
        if v == "ERROR":
            err += 1
            continue
        if v == "UNCERTAIN":
            unc += 1
            continue
        if v == "UNCERTAIN_ZERO_SIGNAL":
            zs += 1
            continue
        # v in {PASS, FAIL}
        if gt == "resolved":
            if v == "PASS":
                tn += 1  # correctly accepted a good patch
            else:
                fp += 1  # false positive: flagged a good patch as FAIL
        elif gt == "unresolved":
            if v == "FAIL":
                tp += 1
            else:
                fn += 1
    return dict(tp=tp, fp=fp, tn=tn, fn=fn, uncertain=unc, zero_signal=zs, error=err, skip=skip)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--only", type=str, default=None, help="Comma-separated instance_ids")
    args = parser.parse_args()

    preds: dict = json.loads(PREDS_JSON.read_text())
    gt_report: dict = json.loads(GT_JSON.read_text())

    resolved_ids = set(gt_report.get("resolved_ids", []))
    unresolved_ids = set(gt_report.get("unresolved_ids", []))
    error_ids = set(gt_report.get("error_ids", []))

    if args.only:
        selected = set(s.strip() for s in args.only.split(",") if s.strip())
        to_process = [iid for iid in preds if iid in selected]
    else:
        to_process = [iid for iid in preds if iid not in error_ids]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    done_ids: set[str] = set()
    results: list[dict] = []
    if args.resume and OUTPUT_JSON.exists():
        results = json.loads(OUTPUT_JSON.read_text())
        done_ids = {r["instance_id"] for r in results}
        print(f"Resume: {len(done_ids)} done")

    remaining = [iid for iid in to_process if iid not in done_ids]
    if remaining:
        print(f"Loading {len(remaining)} instances …")
        instances_list = load_instances(subset="verified", instance_ids=remaining)
        instances_by_id = {inst["instance_id"]: inst for inst in instances_list}

        for i, iid in enumerate(remaining, 1):
            print(f"\n[{i}/{len(remaining)}] {iid}")
            if iid not in instances_by_id:
                results.append({
                    "instance_id": iid,
                    "ground_truth": "resolved" if iid in resolved_ids else "unresolved" if iid in unresolved_ids else "unknown",
                    "verdict": "ERROR",
                    "score": 0.0,
                    "message": "instance not found in dataset",
                    "buckets_used": {},
                    "n_surviving": 0,
                })
                OUTPUT_JSON.write_text(json.dumps(results, indent=2))
                continue

            instance = instances_by_id[iid]
            patch = preds[iid].get("model_patch", "")
            gt_label = (
                "resolved" if iid in resolved_ids
                else "unresolved" if iid in unresolved_ids
                else "unknown"
            )
            row = process_instance(instance, patch, gt_label, timeout=args.timeout)
            results.append(row)
            OUTPUT_JSON.write_text(json.dumps(results, indent=2))

    # Summary
    stats = confusion_stats(results)
    n_surviving = [r["n_surviving"] for r in results if r.get("n_surviving", 0) > 0]
    zero_signal_rate = sum(1 for r in results if r["verdict"] == "UNCERTAIN_ZERO_SIGNAL") / max(1, len(results))

    print("\n" + "=" * 60)
    print("REPRODUCTION CONFUSION MATRIX")
    print("=" * 60)
    print(f"  TP (FAIL on unresolved) : {stats['tp']}")
    print(f"  TN (PASS on resolved)   : {stats['tn']}")
    print(f"  FP (FAIL on resolved)   : {stats['fp']}")
    print(f"  FN (PASS on unresolved) : {stats['fn']}")
    print(f"  UNCERTAIN               : {stats['uncertain']}")
    print(f"  UNCERTAIN_ZERO_SIGNAL   : {stats['zero_signal']}")
    print(f"  ERROR / SKIP            : {stats['error']} / {stats['skip']}")
    print(f"\n  Zero-signal rate        : {zero_signal_rate * 100:.0f}%")
    if n_surviving:
        print(f"  Mean surviving tests    : {sum(n_surviving) / len(n_surviving):.1f}")

    summary = {
        "total": len(results),
        "confusion": stats,
        "zero_signal_rate": zero_signal_rate,
        "mean_surviving_when_nonzero": (sum(n_surviving) / len(n_surviving)) if n_surviving else 0.0,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2))
    print(f"\nResults: {OUTPUT_JSON}")
    print(f"Summary: {SUMMARY_JSON}")


if __name__ == "__main__":
    main()

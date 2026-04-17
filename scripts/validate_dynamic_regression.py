#!/usr/bin/env python3
"""validate_dynamic_regression.py

Validates the Layer-2 regression check from ``sieve/phases/dynamic.py``
against the 50-instance Gemini 2.5 Pro pilot set and reports a confusion
matrix.

Checker evaluated
-----------------
  check_regression_tests — run the PASS_TO_PASS tests from the SWE-bench
    Verified dataset before and after the patch.  Tests that were passing
    before and fail after are counted as regressions (REJECT).  Pre-existing
    failures are ignored (delta approach).

  Unlike ``validate_dynamic_checks.py`` (which heuristically discovers test
  files from changed source paths), this script uses the authoritative
  PASS_TO_PASS list embedded in each SWE-bench instance.

Delta approach
--------------
  1. Run PASS_TO_PASS tests on the UNPATCHED container → pre_failed
  2. Apply the candidate patch
  3. Run the same tests again → post_failed
  4. new_failures = post_failed − pre_failed
  Verdict: REJECT if new_failures is non-empty, PASS otherwise.

Output
------
  runs/dynamic_regression_validation/results.json   per-instance verdicts
  runs/dynamic_regression_validation/summary.json   aggregated confusion matrix
  stdout                                             progress + final report

Usage
-----
  python scripts/validate_dynamic_regression.py           # run all
  python scripts/validate_dynamic_regression.py --resume  # skip already-done
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
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
OUTPUT_DIR  = REPO_ROOT / "runs/dynamic_regression_validation"
OUTPUT_JSON = OUTPUT_DIR / "results.json"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"

# ---------------------------------------------------------------------------
# Imports (after sys.path is set)
# ---------------------------------------------------------------------------
from sieve.phases.dynamic import run_dynamic_checks
from sieve.utils.swebench import load_instances


# ---------------------------------------------------------------------------
# Per-instance processing
# ---------------------------------------------------------------------------

def process_instance(
    instance: dict,
    patch_content: str,
    gt_label: str,
    timeout: int = 120,
) -> dict:
    """Run the regression check for one instance and return a result dict."""
    iid = instance["instance_id"]

    # Parse PASS_TO_PASS for reporting
    raw_p2p = instance.get("PASS_TO_PASS", "[]")
    if isinstance(raw_p2p, list):
        p2p_list = raw_p2p
    else:
        try:
            p2p_list = json.loads(raw_p2p)
        except Exception:
            p2p_list = []

    print(f"  PASS_TO_PASS: {len(p2p_list)} test(s)")

    if not patch_content.strip():
        print("  Empty patch — SKIP")
        return {
            "instance_id":    iid,
            "ground_truth":   gt_label,
            "p2p_count":      len(p2p_list),
            "verdict":        "SKIP",
            "message":        "empty patch",
            "pre_failed":     [],
            "new_failures":   [],
            "error":          "empty patch",
        }

    try:
        result = run_dynamic_checks(
            instance,
            patch_content,
            timeout=timeout,
        )
    except Exception as exc:
        print(f"  [EXCEPTION] {exc}")
        return {
            "instance_id":    iid,
            "ground_truth":   gt_label,
            "p2p_count":      len(p2p_list),
            "verdict":        "ERROR",
            "message":        str(exc),
            "pre_failed":     [],
            "new_failures":   [],
            "error":          str(exc),
        }

    # Extract regression detail from the DynamicCheckResult
    reg_detail = result.details.get("regression_tests")
    verdict  = reg_detail.verdict  if reg_detail else result.verdict
    message  = reg_detail.message  if reg_detail else ""

    # Pull new_failures list out of the message for structured output
    # (the message embeds them; re-parse for clean JSON storage)
    new_failures: list[str] = []
    if verdict == "REJECT" and reg_detail:
        # message format: "N regression(s): a; b; c (+ M more)"
        # We store raw failures only if available in details
        pass  # message already human-readable; failures listed there

    row = {
        "instance_id":    iid,
        "ground_truth":   gt_label,
        "p2p_count":      len(p2p_list),
        "verdict":        verdict,
        "message":        message,
        "new_failures":   new_failures,
        "error":          None,
        "full_result":    result.to_dict(),
    }

    verdict_label = verdict
    suffix = f" — {message[:80]}" if verdict not in ("PASS", "SKIP") else ""
    print(f"  {verdict_label}{suffix}")
    return row


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def confusion_stats(results: list[dict]) -> dict:
    """Compute TP/FP/TN/FN for the regression check."""
    tp = fp = tn = fn = skip = err = 0
    for r in results:
        gt = r["ground_truth"]
        v  = r["verdict"]
        if v in ("SKIP", "ERROR"):
            if v == "SKIP":
                skip += 1
            else:
                err += 1
            continue
        if gt == "resolved":    # correct patch — should PASS
            if v == "REJECT":
                fp += 1         # false positive: rejected a good patch
            else:
                tn += 1         # true negative: correctly passed
        else:                   # unresolved / incorrect patch — should REJECT
            if v == "REJECT":
                tp += 1         # true positive: caught bad patch
            else:
                fn += 1         # false negative: bad patch slipped through
    return dict(tp=tp, fp=fp, tn=tn, fn=fn, skip=skip, err=err)


def print_confusion(stats: dict, resolved_n: int, unresolved_n: int) -> None:
    tp, fp, tn, fn = stats["tp"], stats["fp"], stats["tn"], stats["fn"]
    print(f"\n{'─'*60}")
    print("  check_regression_tests  (PASS_TO_PASS delta)")
    print(f"{'─'*60}")
    print(f"  Ground-truth resolved   ({resolved_n:2d} patches):")
    print(f"    PASS (correct)  : {tn:2d}  TN")
    print(f"    REJECT          : {fp:2d}  ← false positive")
    print(f"  Ground-truth unresolved ({unresolved_n:2d} patches):")
    print(f"    REJECT (caught) : {tp:2d}  TP")
    print(f"    PASS (missed)   : {fn:2d}  FN")
    if stats["skip"] or stats["err"]:
        print(f"  SKIP / ERROR    : {stats['skip']} / {stats['err']}")
    total_bad = tp + fn
    if total_bad:
        print(f"  Catch rate  : {tp}/{total_bad} ({tp / total_bad * 100:.0f}%)")
    total_good = tn + fp
    if total_good:
        print(f"  Hard FP rate: {fp}/{total_good} ({fp / total_good * 100:.0f}%)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip instances already present in results.json",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Seconds allowed per test run (pre-patch and post-patch each). Default: 120",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Load preds + ground-truth labels
    # ------------------------------------------------------------------
    print("Loading preds.json …")
    preds: dict = json.loads(PREDS_JSON.read_text())

    print("Loading ground-truth report …")
    gt_report: dict = json.loads(GT_JSON.read_text())

    resolved_ids   = set(gt_report.get("resolved_ids",   []))
    unresolved_ids = set(gt_report.get("unresolved_ids", []))
    error_ids      = set(gt_report.get("error_ids",      []))

    to_process = [iid for iid in preds if iid not in error_ids]

    print(f"\nTotal predictions       : {len(preds)}")
    print(f"Skipping harness errors : {sorted(error_ids)}")
    print(f"To process              : {len(to_process)}")

    # ------------------------------------------------------------------
    # Resume: load already-completed results
    # ------------------------------------------------------------------
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    done_ids: set[str] = set()
    results: list[dict] = []

    if args.resume and OUTPUT_JSON.exists():
        results = json.loads(OUTPUT_JSON.read_text())
        done_ids = {r["instance_id"] for r in results}
        print(f"Resume: {len(done_ids)} already done, skipping them\n")
    else:
        print()

    # ------------------------------------------------------------------
    # Load SWE-bench instances (needed for PASS_TO_PASS)
    # ------------------------------------------------------------------
    remaining = [iid for iid in to_process if iid not in done_ids]
    if not remaining:
        print("All instances already processed.")
    else:
        print(f"Loading {len(remaining)} SWE-bench instance(s) from HuggingFace …")
        instances_list = load_instances(
            subset="verified",
            instance_ids=remaining,
        )
        instances_by_id = {inst["instance_id"]: inst for inst in instances_list}

        missing = set(remaining) - set(instances_by_id)
        if missing:
            print(f"WARNING: {len(missing)} instance(s) not found in dataset: {sorted(missing)}")

        # ------------------------------------------------------------------
        # Process each instance
        # ------------------------------------------------------------------
        for i, iid in enumerate(remaining, 1):
            print(f"\n{'='*60}")
            print(f"[{i}/{len(remaining)}] {iid}")

            if iid not in instances_by_id:
                print("  Not found in SWE-bench dataset — skipping")
                results.append({
                    "instance_id":  iid,
                    "ground_truth": "resolved" if iid in resolved_ids else
                                    "unresolved" if iid in unresolved_ids else "unknown",
                    "p2p_count":    0,
                    "verdict":      "ERROR",
                    "message":      "instance not found in SWE-bench dataset",
                    "new_failures": [],
                    "error":        "not in dataset",
                })
                OUTPUT_JSON.write_text(json.dumps(results, indent=2))
                continue

            instance  = instances_by_id[iid]
            patch     = preds[iid].get("model_patch", "")
            gt_label  = (
                "resolved"   if iid in resolved_ids   else
                "unresolved" if iid in unresolved_ids else
                "unknown"
            )

            row = process_instance(instance, patch, gt_label, timeout=args.timeout)
            results.append(row)

            # Incremental save after every instance
            OUTPUT_JSON.write_text(json.dumps(results, indent=2))

    # ------------------------------------------------------------------
    # Final report
    # ------------------------------------------------------------------
    resolved_results   = [r for r in results if r["ground_truth"] == "resolved"]
    unresolved_results = [r for r in results if r["ground_truth"] == "unresolved"]
    rn = len(resolved_results)
    un = len(unresolved_results)

    print("\n\n" + "=" * 60)
    print("RESULTS TABLE")
    print("=" * 60)
    header = (
        f"{'instance_id':<45} {'gt':<12} {'p2p':>4}  {'verdict':<8}"
    )
    print(header)
    print("-" * len(header))
    for r in sorted(results, key=lambda x: x["instance_id"]):
        suffix = f"  — {r['message'][:55]}" if r["verdict"] not in ("PASS", "SKIP") else ""
        print(
            f"{r['instance_id']:<45} {r['ground_truth']:<12} "
            f"{r['p2p_count']:>4}  {r['verdict']:<8}{suffix}"
        )

    # ------------------------------------------------------------------
    # Confusion matrix
    # ------------------------------------------------------------------
    print("\n\n" + "=" * 60)
    print("CONFUSION MATRIX")
    print("=" * 60)
    stats = confusion_stats(results)
    print_confusion(stats, rn, un)

    # ------------------------------------------------------------------
    # PASS_TO_PASS coverage
    # ------------------------------------------------------------------
    p2p_counts = [r["p2p_count"] for r in results if r["p2p_count"] > 0]
    skips = [r for r in results if r["verdict"] == "SKIP"]
    print(f"\n{'='*60}")
    print("PASS_TO_PASS COVERAGE")
    print(f"{'='*60}")
    print(f"  Instances with ≥1 test : {len(p2p_counts)}/{len(results)}")
    if p2p_counts:
        print(f"  Min / Median / Max tests: "
              f"{min(p2p_counts)} / "
              f"{sorted(p2p_counts)[len(p2p_counts)//2]} / "
              f"{max(p2p_counts)}")
    print(f"  Instances skipped (0 tests) : {len(skips)}")
    for r in skips:
        print(f"    {r['instance_id']} ({r['ground_truth']})")

    # ------------------------------------------------------------------
    # False positives
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("FALSE POSITIVES  (REJECT on a correctly-resolved patch)")
    print(f"{'='*60}")
    fps = [r for r in resolved_results if r["verdict"] == "REJECT"]
    if fps:
        for r in fps:
            print(f"  {r['instance_id']} — {r['message'][:100]}")
    else:
        print("  None.")

    # ------------------------------------------------------------------
    # True positives
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TRUE POSITIVES  (REJECT on an unresolved patch)")
    print(f"{'='*60}")
    tps = [r for r in unresolved_results if r["verdict"] == "REJECT"]
    if tps:
        for r in tps:
            print(f"  {r['instance_id']} — {r['message'][:100]}")
    else:
        print("  None.")

    # ------------------------------------------------------------------
    # False negatives (bad patches that slipped through)
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("FALSE NEGATIVES  (bad patches not caught)")
    print(f"{'='*60}")
    fns = [r for r in unresolved_results if r["verdict"] == "PASS"]
    if fns:
        for r in fns:
            print(f"  {r['instance_id']}  p2p={r['p2p_count']}")
    else:
        print("  None.")

    # ------------------------------------------------------------------
    # Save summary
    # ------------------------------------------------------------------
    summary = {
        "total_processed":        len(results),
        "resolved":               rn,
        "unresolved":             un,
        "p2p_coverage": {
            "with_tests":         len(p2p_counts),
            "without_tests":      len(results) - len(p2p_counts),
            "min_tests":          min(p2p_counts) if p2p_counts else 0,
            "max_tests":          max(p2p_counts) if p2p_counts else 0,
            "median_tests":       sorted(p2p_counts)[len(p2p_counts)//2] if p2p_counts else 0,
        },
        "check_regression_tests": stats,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2))

    print(f"\nFull results : {OUTPUT_JSON}")
    print(f"Summary      : {SUMMARY_JSON}")


if __name__ == "__main__":
    main()

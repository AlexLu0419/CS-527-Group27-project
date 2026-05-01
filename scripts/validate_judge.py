#!/usr/bin/env python3
"""validate_judge.py

Runs Layer 3 (judge) against the UNCERTAIN-bucket patches found by
``validate_reproduction.py``. Requires ``runs/reproduction_validation/results.json``
to exist.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_PREDS = (
    REPO_ROOT
    / "runs/swe-verified_50_gemini-2.5-pro"
    / "swe_verified_50_gemini-2.5-pro-new"
    / "preds.json"
)
DEFAULT_REPRO = REPO_ROOT / "runs/reproduction_validation/results.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "runs/judge_validation"

PREDS_JSON: Path = DEFAULT_PREDS
REPRO_JSON: Path = DEFAULT_REPRO
OUTPUT_DIR: Path = DEFAULT_OUTPUT_DIR
OUTPUT_JSON: Path = OUTPUT_DIR / "results.json"
SUMMARY_JSON: Path = OUTPUT_DIR / "summary.json"

from sieve.judge.aggregate import aggregate
from sieve.judge.assemble import assemble_judge_input
from sieve.phases.judge import run_judge
from sieve.phases.reproduction import PerTestResult, ReproductionVerdict
from sieve.repro.cache import phase_a
from sieve.utils.swebench import load_instances


def _rebuild_repro(row: dict) -> ReproductionVerdict:
    per = [PerTestResult(**pt) for pt in row.get("per_test", [])]
    return ReproductionVerdict(
        verdict=row["verdict"],
        score=row.get("score", 0.0),
        per_test=per,
        buckets_used=row.get("buckets_used", {}),
        message=row.get("message", ""),
        feedback=row.get("feedback"),
    )


def main() -> None:
    global PREDS_JSON, REPRO_JSON, OUTPUT_DIR, OUTPUT_JSON, SUMMARY_JSON
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--only", type=str, default=None)
    parser.add_argument("--preds", type=Path, default=DEFAULT_PREDS,
                        help="Path to preds.json (mini-swe-agent output)")
    parser.add_argument("--repro", type=Path, default=DEFAULT_REPRO,
                        help="Path to reproduction results.json")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="Directory for results.json / summary.json")
    args = parser.parse_args()

    PREDS_JSON = args.preds
    REPRO_JSON = args.repro
    OUTPUT_DIR = args.output_dir
    OUTPUT_JSON = OUTPUT_DIR / "results.json"
    SUMMARY_JSON = OUTPUT_DIR / "summary.json"

    print(f"Preds : {PREDS_JSON}")
    print(f"Repro : {REPRO_JSON}")
    print(f"Output: {OUTPUT_DIR}")

    if not REPRO_JSON.exists():
        print(f"ERROR: {REPRO_JSON} missing. Run validate_reproduction.py first.", file=sys.stderr)
        sys.exit(2)

    repro_rows = json.loads(REPRO_JSON.read_text())
    preds = json.loads(PREDS_JSON.read_text())

    roster = [r for r in repro_rows if r["verdict"] in ("UNCERTAIN", "UNCERTAIN_ZERO_SIGNAL")]

    if args.only:
        sel = set(s.strip() for s in args.only.split(",") if s.strip())
        roster = [r for r in roster if r["instance_id"] in sel]

    n_uncertain = sum(1 for r in roster if r["verdict"] == "UNCERTAIN")
    n_zero_signal = sum(1 for r in roster if r["verdict"] == "UNCERTAIN_ZERO_SIGNAL")
    print(f"Patches to judge: {len(roster)} (UNCERTAIN={n_uncertain}, "
          f"UNCERTAIN_ZERO_SIGNAL={n_zero_signal})")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    done: set[str] = set()
    results: list[dict] = []
    if args.resume and OUTPUT_JSON.exists():
        results = json.loads(OUTPUT_JSON.read_text())
        done = {r["instance_id"] for r in results}

    remaining = [r for r in roster if r["instance_id"] not in done]
    if remaining:
        instances_list = load_instances(subset="verified", instance_ids=[r["instance_id"] for r in remaining])
        instances_by_id = {inst["instance_id"]: inst for inst in instances_list}

        for i, r in enumerate(remaining, 1):
            iid = r["instance_id"]
            print(f"\n[{i}/{len(remaining)}] {iid}")
            instance = instances_by_id.get(iid)
            if instance is None:
                results.append({"instance_id": iid, "error": "not_in_dataset"})
                OUTPUT_JSON.write_text(json.dumps(results, indent=2))
                continue

            patch = preds[iid].get("model_patch", "")
            try:
                cache = phase_a(instance)
                repro = _rebuild_repro(r)
                ji = assemble_judge_input(instance, patch, repro, cache)
                jo = run_judge(ji)
                fv = aggregate(repro, jo)
            except Exception as exc:
                print(f"  [EXCEPTION] {exc}")
                results.append({"instance_id": iid, "ground_truth": r["ground_truth"], "error": str(exc)})
                OUTPUT_JSON.write_text(json.dumps(results, indent=2))
                continue

            row = {
                "instance_id": iid,
                "ground_truth": r["ground_truth"],
                "repro_verdict": repro.verdict,
                "repro_score": repro.score,
                "judge": jo.to_dict(),
                "final": fv.to_dict(),
            }
            results.append(row)
            OUTPUT_JSON.write_text(json.dumps(results, indent=2))
            print(f"  judge={jo.verdict} conf={jo.confidence} S={jo.weighted_score:.2f} → final={fv.verdict} ({fv.source})")

    # Summary
    tp = fp = tn = fn = unc = err = 0
    for r in results:
        if "error" in r and "judge" not in r:
            err += 1
            continue
        gt = r["ground_truth"]
        final = r["final"]["verdict"]
        if final == "UNCERTAIN":
            unc += 1
            continue
        if gt == "resolved":
            if final == "PASS":
                tn += 1
            else:
                fp += 1
        elif gt == "unresolved":
            if final == "FAIL":
                tp += 1
            else:
                fn += 1

    print("\n" + "=" * 60)
    print("JUDGE CASCADE CONFUSION (on UNCERTAIN bucket)")
    print("=" * 60)
    print(f"  TP={tp}  TN={tn}  FP={fp}  FN={fn}  UNCERTAIN={unc}  ERROR={err}")

    summary = {
        "total": len(results),
        "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn, "uncertain": unc, "error": err},
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2))
    print(f"\nResults: {OUTPUT_JSON}")
    print(f"Summary: {SUMMARY_JSON}")


if __name__ == "__main__":
    main()

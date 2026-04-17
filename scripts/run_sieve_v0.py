#!/usr/bin/env python3
"""run_sieve_v0.py — offline cascade driver.

Chains static → regression → reproduction → judge on an existing preds.json,
using cached Layer 1/2a results when available and writing a per-instance
snapshot record suitable for evolution cycle 1.

Usage
-----
  python scripts/run_sieve_v0.py \
      --manifest runs/evolution/slice_manifest.json \
      --split evolution \
      --out runs/evolution/sieve_v0_evolution_results.json \
      [--reuse-static] [--reuse-regression] [--only <iid,iid,...>]

If ``--manifest`` is omitted, runs on all labeled (resolved+unresolved) instances.
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
STATIC_CACHE = REPO_ROOT / "runs/static_checks_validation/results.json"
REG_CACHE = REPO_ROOT / "runs/dynamic_regression_validation/results.json"

from sieve.evolution.versions import all_layer_snapshots
from sieve.judge.aggregate import aggregate
from sieve.judge.assemble import assemble_judge_input
from sieve.phases.dynamic import run_dynamic_checks
from sieve.phases.judge import run_judge
from sieve.phases.reproduction import run_reproduction_checks
from sieve.phases.static import run_static_checks  # noqa: F401 (kept for parity; not used in offline re-score)
from sieve.repro.cache import phase_a
from sieve.utils.swebench import load_instances


def _load_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {r["instance_id"]: r for r in raw}


def _static_verdict(row: dict) -> tuple[str, str]:
    """Aggregate the 3 static sub-checks into a single verdict."""
    if row.get("error"):
        return "ERROR", row["error"]
    sub = [row.get("check_patch_applies", {}),
           row.get("check_files_parse", {}),
           row.get("check_lint_delta", {})]
    if any(s.get("verdict") == "REJECT" for s in sub):
        worst = next(s for s in sub if s.get("verdict") == "REJECT")
        return "REJECT", worst.get("message", "")
    if any(s.get("verdict") == "FLAG" for s in sub):
        return "FLAG", "; ".join(s.get("message", "") for s in sub if s.get("verdict") == "FLAG")
    return "PASS", ""


def _process(
    instance: dict,
    patch: str,
    gt: str,
    *,
    static_row: dict | None,
    reg_row: dict | None,
    reuse_static: bool,
    reuse_regression: bool,
    snapshots: dict[str, str],
) -> dict:
    iid = instance["instance_id"]
    record: dict = {
        "instance_id": iid,
        "ground_truth": gt,
        "layer_snapshots": snapshots,
        "layers": {},
    }

    # ---- Layer 1: static ----
    if reuse_static and static_row is not None:
        verdict, msg = _static_verdict(static_row)
        record["layers"]["static"] = {"verdict": verdict, "message": msg, "source": "cached"}
    else:
        record["layers"]["static"] = {"verdict": "SKIP", "message": "re-run not implemented offline", "source": "skipped"}

    # ---- Layer 2a: regression ----
    if reuse_regression and reg_row is not None:
        record["layers"]["regression"] = {
            "verdict": reg_row.get("verdict", "ERROR"),
            "message": reg_row.get("message", ""),
            "source": "cached",
        }
    else:
        try:
            dc = run_dynamic_checks(instance, patch)
            record["layers"]["regression"] = {
                "verdict": dc.verdict,
                "message": dc.details.get("regression_tests", None).message if dc.details.get("regression_tests") else "",
                "source": "computed",
            }
        except Exception as exc:
            record["layers"]["regression"] = {"verdict": "ERROR", "message": str(exc), "source": "computed"}

    # Cascade cutoff: if static or regression REJECT → final FAIL; no repro/judge
    if record["layers"]["static"]["verdict"] == "REJECT":
        record["final_verdict"] = "FAIL"
        record["final_source"] = "static"
        return record
    if record["layers"]["regression"]["verdict"] == "REJECT":
        record["final_verdict"] = "FAIL"
        record["final_source"] = "regression"
        return record

    # ---- Layer 2b: reproduction ----
    try:
        cache = phase_a(instance)
        repro = run_reproduction_checks(instance, patch, cache=cache)
    except Exception as exc:
        record["layers"]["reproduction"] = {"verdict": "ERROR", "message": str(exc), "source": "computed"}
        record["final_verdict"] = "UNCERTAIN"
        record["final_source"] = "repro_error"
        return record

    record["layers"]["reproduction"] = {
        "verdict": repro.verdict,
        "score": repro.score,
        "buckets_used": repro.buckets_used,
        "feedback": repro.feedback,
        "message": repro.message,
        "source": "computed",
    }

    if repro.verdict in ("PASS", "FAIL"):
        record["final_verdict"] = repro.verdict
        record["final_source"] = "repro"
        return record

    # ---- Layer 3: judge (only on UNCERTAIN, not on UNCERTAIN_ZERO_SIGNAL) ----
    if repro.verdict == "UNCERTAIN_ZERO_SIGNAL":
        record["layers"]["judge"] = None
        record["final_verdict"] = "UNCERTAIN"
        record["final_source"] = "judge_skipped"
        return record

    try:
        ji = assemble_judge_input(instance, patch, repro, cache)
        judge = run_judge(ji)
        fv = aggregate(repro, judge)
    except Exception as exc:
        record["layers"]["judge"] = {"error": str(exc)}
        record["final_verdict"] = "UNCERTAIN"
        record["final_source"] = "judge_error"
        return record

    record["layers"]["judge"] = {
        "verdict": judge.verdict,
        "confidence": judge.confidence,
        "weighted_score": judge.weighted_score,
        "rubric_scores": judge.rubric_scores,
        "parse_error": judge.parse_error,
        "source": "computed",
    }
    record["final_verdict"] = fv.verdict
    record["final_source"] = fv.source
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=str, default=None)
    parser.add_argument("--split", type=str, default="evolution", choices=["evolution", "held_out", "all"])
    parser.add_argument("--out", type=str, required=True)
    parser.add_argument("--reuse-static", action="store_true", default=True)
    parser.add_argument("--reuse-regression", action="store_true", default=True)
    parser.add_argument("--only", type=str, default=None)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    preds = json.loads(PREDS_JSON.read_text())
    gt_report = json.loads(GT_JSON.read_text())
    resolved_ids = set(gt_report.get("resolved_ids", []))
    unresolved_ids = set(gt_report.get("unresolved_ids", []))
    error_ids = set(gt_report.get("error_ids", []))

    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text())
        if args.split == "evolution":
            slice_ = manifest["evolution_slice"]
        elif args.split == "held_out":
            slice_ = manifest["held_out"]
        else:
            slice_ = manifest["evolution_slice"] + manifest["held_out"]
        target_ids = [s["instance_id"] for s in slice_]
    else:
        target_ids = [iid for iid in preds if iid not in error_ids]

    if args.only:
        sel = set(s.strip() for s in args.only.split(",") if s.strip())
        target_ids = [iid for iid in target_ids if iid in sel]

    static_cache = _load_cache(STATIC_CACHE)
    reg_cache = _load_cache(REG_CACHE)
    snapshots = all_layer_snapshots()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    records: list[dict] = []
    if args.resume and out_path.exists():
        records = json.loads(out_path.read_text())
        done = {r["instance_id"] for r in records}
        print(f"Resume: {len(done)} already done")

    remaining = [iid for iid in target_ids if iid not in done]
    if remaining:
        print(f"Loading {len(remaining)} instances …")
        insts = load_instances(subset="verified", instance_ids=remaining)
        insts_by_id = {i["instance_id"]: i for i in insts}

        for i, iid in enumerate(remaining, 1):
            print(f"\n[{i}/{len(remaining)}] {iid}")
            if iid not in insts_by_id:
                records.append({"instance_id": iid, "error": "not_in_dataset"})
                out_path.write_text(json.dumps(records, indent=2))
                continue
            patch = preds[iid].get("model_patch", "")
            gt = (
                "resolved" if iid in resolved_ids
                else "unresolved" if iid in unresolved_ids
                else "unknown"
            )
            rec = _process(
                insts_by_id[iid],
                patch,
                gt,
                static_row=static_cache.get(iid),
                reg_row=reg_cache.get(iid),
                reuse_static=args.reuse_static,
                reuse_regression=args.reuse_regression,
                snapshots=snapshots,
            )
            records.append(rec)
            out_path.write_text(json.dumps(records, indent=2))
            print(f"  final={rec.get('final_verdict')} source={rec.get('final_source')} gt={gt}")

    # Summary
    tp = fp = tn = fn = unc = err = 0
    for r in records:
        fv = r.get("final_verdict")
        gt = r.get("ground_truth")
        if fv is None:
            err += 1
            continue
        if fv == "UNCERTAIN":
            unc += 1
            continue
        if fv == "ERROR":
            err += 1
            continue
        if gt == "resolved":
            if fv == "PASS":
                tn += 1
            else:
                fp += 1
        elif gt == "unresolved":
            if fv == "FAIL":
                tp += 1
            else:
                fn += 1

    print("\n" + "=" * 60)
    print(f"CASCADE v0 — split={args.split} — n={len(records)}")
    print("=" * 60)
    print(f"  TP={tp}  TN={tn}  FP={fp}  FN={fn}  UNCERTAIN={unc}  ERROR={err}")
    print(f"  Results → {out_path}")


if __name__ == "__main__":
    main()

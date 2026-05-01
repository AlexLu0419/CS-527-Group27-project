#!/usr/bin/env python3
"""validate_static_checks.py

Runs the Layer-1 static checker against the 50-instance Gemini 2.5 Pro pilot
set and reports a confusion matrix.

Checker evaluated
-----------------
1. check_patch_applies  — git apply --check (dry-run, no disk changes)

The check is executed via Docker exec inside the per-instance SWE-bench
container so the repo is in exactly the state expected by the pipeline.

Usage
-----
    python scripts/validate_static_checks.py

Output
------
    runs/static_checks_validation/results.json   per-instance verdicts
    runs/static_checks_validation/summary.json   aggregated confusion matrix
    stdout                                        progress + final report
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (defaults; override with CLI flags)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PREDS = (
    REPO_ROOT
    / "runs/swe-verified_50_gemini-2.5-pro"
    / "swe_verified_50_gemini-2.5-pro-new"
    / "preds.json"
)
DEFAULT_GT = (
    REPO_ROOT
    / "runs/sb-cli-reports"
    / "gemini__gemini-2.5-pro.gemini-2.5-pro-mini50-run.json"
)
DEFAULT_OUTPUT_DIR = REPO_ROOT / "runs/static_checks_validation"

# These get rebound in main() once flags are parsed.
PREDS_JSON: Path = DEFAULT_PREDS
GT_JSON: Path = DEFAULT_GT
OUTPUT_DIR: Path = DEFAULT_OUTPUT_DIR
OUTPUT_JSON: Path = OUTPUT_DIR / "results.json"
SUMMARY_JSON: Path = OUTPUT_DIR / "summary.json"


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def image_name(instance_id: str) -> str:
    transformed = instance_id.replace("__", "_1776_").lower()
    return f"swebench/sweb.eval.x86_64.{transformed}:latest"


def run_cmd(cmd: list[str], timeout: int = 120, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)


def start_container(img: str) -> str | None:
    try:
        r = run_cmd(
            ["docker", "run", "-d", "--rm", "--platform", "linux/amd64", img, "sleep", "2h"],
            timeout=180,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def stop_container(cid: str) -> None:
    try:
        subprocess.run(["docker", "stop", cid], capture_output=True, timeout=30)
    except Exception:
        pass


def docker_exec(cid: str, cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return run_cmd(["docker", "exec", cid, *cmd], timeout=timeout)


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------

def extract_modified_py_files(patch: str) -> list[str]:
    """Return .py paths modified by the patch (relative, no 'b/' prefix)."""
    files: list[str] = []
    seen: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[4:].strip().split("\t")[0]   # strip trailing timestamp
        if path.startswith("b/"):
            path = path[2:]
        if path in ("/dev/null", "") or not path.endswith(".py"):
            continue
        if path not in seen:
            seen.add(path)
            files.append(path)
    return files


def copy_patch_to_container(cid: str, patch_content: str) -> tuple[bool, str]:
    """Write patch to a temp file and docker-cp it to /tmp/patch.diff."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
        f.write(patch_content)
        local = f.name
    try:
        r = run_cmd(["docker", "cp", local, f"{cid}:/tmp/patch.diff"], timeout=30)
        return (r.returncode == 0, r.stderr.strip())
    finally:
        os.unlink(local)


# ---------------------------------------------------------------------------
# Checker implementation (run via docker exec)
# ---------------------------------------------------------------------------

def check_patch_applies(cid: str) -> dict:
    """
    Verdict: REJECT if git apply --check fails, PASS otherwise.
    Does NOT modify the working tree.
    """
    r = docker_exec(cid, ["git", "-C", "/testbed", "apply", "--check", "/tmp/patch.diff"])
    if r.returncode == 0:
        return {"verdict": "PASS", "message": ""}
    msg = (r.stderr or r.stdout).strip()
    return {"verdict": "REJECT", "message": msg}


# ---------------------------------------------------------------------------
# Per-instance processing
# ---------------------------------------------------------------------------

def process_instance(instance_id: str, patch: str, ground_truth: str) -> dict:
    """Run the patch-applies check on one instance. Returns result dict."""
    py_files = extract_modified_py_files(patch)

    result: dict = {
        "instance_id": instance_id,
        "ground_truth": ground_truth,
        "modified_py_files": py_files,
        "check_patch_applies": {"verdict": "SKIP", "message": ""},
        "error": None,
    }

    img = image_name(instance_id)
    cid = start_container(img)
    if cid is None:
        result["error"] = "container start failed"
        result["check_patch_applies"] = {"verdict": "ERROR", "message": "container start failed"}
        return result

    print(f"  Container {cid[:12]} started")
    try:
        # Copy patch into container
        ok, err = copy_patch_to_container(cid, patch)
        if not ok:
            result["error"] = f"docker cp patch failed: {err}"
            result["check_patch_applies"] = {"verdict": "ERROR", "message": result["error"]}
            return result

        # ---- check_patch_applies (dry-run, patch NOT applied) ----
        cpa = check_patch_applies(cid)
        result["check_patch_applies"] = cpa
        print(f"  check_patch_applies: {cpa['verdict']}"
              + (f" — {cpa['message'][:80]}" if cpa["verdict"] == "REJECT" else ""))

    except Exception as e:
        result["error"] = str(e)
        print(f"  [EXCEPTION] {e}")
    finally:
        stop_container(cid)
        print(f"  Container stopped")

    return result


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

VERDICT_ORDER = ("REJECT", "PASS", "SKIP", "ERROR")


def confusion_stats(results: list[dict]) -> dict:
    """Compute TP/FP/TN/FN counts for check_patch_applies."""
    tp = fp = tn = fn = skip = err = 0
    for r in results:
        gt = r["ground_truth"]
        v = r["check_patch_applies"]["verdict"]
        if v == "SKIP":
            skip += 1
            continue
        if v == "ERROR":
            err += 1
            continue
        if gt == "resolved":       # correct patch
            if v == "REJECT":
                fp += 1            # false positive: rejected a good patch
            else:
                tn += 1            # true negative: correctly passed
        else:                      # unresolved / incorrect patch
            if v == "REJECT":
                tp += 1            # true positive: correctly rejected a bad patch
            else:
                fn += 1            # false negative: bad patch slipped through
    return dict(tp=tp, fp=fp, tn=tn, fn=fn, skip=skip, err=err)


def print_confusion(stats: dict, resolved_n: int, unresolved_n: int) -> None:
    tp, fp, tn, fn = stats["tp"], stats["fp"], stats["tn"], stats["fn"]

    print(f"\n{'─'*60}")
    print(f"  check_patch_applies")
    print(f"{'─'*60}")
    print(f"  Ground-truth resolved   ({resolved_n:2d} patches):")
    print(f"    PASS (correct)  : {tn:2d}")
    print(f"    REJECT          : {fp:2d}  ← false positive")
    print(f"  Ground-truth unresolved ({unresolved_n:2d} patches):")
    print(f"    REJECT (caught) : {tp:2d}  ← true positive")
    print(f"    PASS (missed)   : {fn:2d}  ← false negative")
    if stats["skip"] or stats["err"]:
        print(f"  SKIP / ERROR    : {stats['skip']} / {stats['err']}")

    total_bad = tp + fn
    if total_bad:
        catch = tp / total_bad * 100
        print(f"  Catch rate (bad patches caught by REJECT):     {tp}/{total_bad} ({catch:.0f}%)")
    total_good = tn + fp
    if total_good:
        fpr = fp / total_good * 100
        print(f"  Hard FP rate   (REJECT on good patch):         {fp}/{total_good} ({fpr:.0f}%)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--preds", type=Path, default=DEFAULT_PREDS,
                   help="Path to preds.json (mini-swe-agent output)")
    p.add_argument("--gt", type=Path, default=DEFAULT_GT,
                   help="Path to SWE-bench ground-truth report JSON")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                   help="Directory for results.json / summary.json")
    return p.parse_args()


def main() -> None:
    global PREDS_JSON, GT_JSON, OUTPUT_DIR, OUTPUT_JSON, SUMMARY_JSON
    args = parse_args()
    PREDS_JSON = args.preds
    GT_JSON = args.gt
    OUTPUT_DIR = args.output_dir
    OUTPUT_JSON = OUTPUT_DIR / "results.json"
    SUMMARY_JSON = OUTPUT_DIR / "summary.json"

    print(f"Preds : {PREDS_JSON}")
    print(f"GT    : {GT_JSON}")
    print(f"Output: {OUTPUT_DIR}")
    print("Loading preds.json …")
    preds: dict = json.loads(PREDS_JSON.read_text())

    print("Loading ground-truth report …")
    gt: dict = json.loads(GT_JSON.read_text())

    resolved_ids   = set(gt.get("resolved_ids", []))
    unresolved_ids = set(gt.get("unresolved_ids", []))
    error_ids      = set(gt.get("error_ids", []))

    to_process = [iid for iid in preds if iid not in error_ids]

    print(f"\nTotal predictions : {len(preds)}")
    print(f"Skipping (harness errors): {sorted(error_ids)}")
    print(f"Processing {len(to_process)} instances\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []

    for i, iid in enumerate(to_process, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(to_process)}] {iid}")

        patch = preds[iid].get("model_patch", "")
        if iid in resolved_ids:
            gt_label = "resolved"
        elif iid in unresolved_ids:
            gt_label = "unresolved"
        else:
            gt_label = "unknown"

        if not patch.strip():
            print("  Empty patch — skipping")
            results.append({
                "instance_id": iid, "ground_truth": gt_label,
                "modified_py_files": [],
                "check_patch_applies": {"verdict": "PASS", "message": "empty patch"},
                "error": "empty patch",
            })
        else:
            try:
                r = process_instance(iid, patch, gt_label)
            except Exception as e:
                print(f"  [EXCEPTION] {e}")
                r = {
                    "instance_id": iid, "ground_truth": gt_label,
                    "modified_py_files": [],
                    "check_patch_applies": {"verdict": "ERROR", "message": str(e)},
                    "error": str(e),
                }
            results.append(r)

        # Save incrementally
        OUTPUT_JSON.write_text(json.dumps(results, indent=2))

    # -----------------------------------------------------------------------
    # Final report
    # -----------------------------------------------------------------------
    resolved_results   = [r for r in results if r["ground_truth"] == "resolved"]
    unresolved_results = [r for r in results if r["ground_truth"] == "unresolved"]
    rn = len(resolved_results)
    un = len(unresolved_results)

    print("\n\n" + "="*60)
    print("RESULTS TABLE")
    print("="*60)
    header = f"{'instance_id':<45} {'gt':<12} {'applies':<9}"
    print(header)
    print("-" * len(header))
    for r in results:
        iid = r["instance_id"]
        gt  = r["ground_truth"]
        v   = r["check_patch_applies"]["verdict"]
        print(f"{iid:<45} {gt:<12} {v:<9}")

    print("\n\n" + "="*60)
    print("CONFUSION MATRIX")
    print("="*60)

    stats = confusion_stats(results)
    print_confusion(stats, rn, un)

    # False positives (REJECT on resolved)
    print(f"\n{'='*60}")
    print("FALSE POSITIVES  (REJECT on a correctly-resolved patch)")
    print(f"{'='*60}")
    fps = [r for r in resolved_results if r["check_patch_applies"]["verdict"] == "REJECT"]
    if fps:
        for r in fps:
            print(f"  {r['instance_id']} — {r['check_patch_applies']['message'][:100]}")
    else:
        print("  None.")

    # Save summary
    summary = {
        "total_processed": len(results),
        "resolved": rn,
        "unresolved": un,
        "check_patch_applies": stats,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2))

    print(f"\nFull results : {OUTPUT_JSON}")
    print(f"Summary      : {SUMMARY_JSON}")


if __name__ == "__main__":
    main()

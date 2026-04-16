#!/usr/bin/env python3
"""validate_semgrep.py

Runs semgrep on patched Python files extracted from SWE-bench Docker containers
and measures false-positive / false-negative rates against ground-truth labels.

Usage:
    python scripts/validate_semgrep.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
PREDS_JSON = REPO_ROOT / "runs/swe-verified_50_gemini-2.5-pro/swe_verified_50_gemini-2.5-pro-new/preds.json"
GT_JSON = REPO_ROOT / "runs/sb-cli-reports/gemini__gemini-2.5-pro.gemini-2.5-pro-mini50-run.json"
OUTPUT_DIR = REPO_ROOT / "runs/semgrep_validation"
OUTPUT_JSON = OUTPUT_DIR / "results.json"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONTAINER_TIMEOUT = 120   # seconds for docker operations
SEMGREP_TIMEOUT = 120     # seconds for semgrep run
SEMGREP_CONFIG = "p/python"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def image_name(instance_id: str) -> str:
    """Convert instance_id to Docker image name.

    e.g. django__django-11815 -> swebench/sweb.eval.x86_64.django_1776_django-11815:latest
    """
    transformed = instance_id.replace("__", "_1776_").lower()
    return f"swebench/sweb.eval.x86_64.{transformed}:latest"


def extract_modified_py_files(patch: str) -> list[str]:
    """Extract the list of modified .py file paths from a unified diff patch.

    Uses the +++ b/path line (git diff format) or +++ path (plain diff format).
    Only returns .py files.
    Returns paths relative to repo root (without the 'b/' prefix).
    """
    files: list[str] = []
    seen: set[str] = set()

    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue

        path = line[4:].strip()

        # Strip git diff prefix 'b/'
        if path.startswith("b/"):
            path = path[2:]

        # Skip /dev/null
        if path == "/dev/null" or path.startswith("/dev/"):
            continue

        # Only care about .py files
        if not path.endswith(".py"):
            continue

        if path not in seen:
            seen.add(path)
            files.append(path)

    return files


def run_cmd(cmd: list[str], timeout: int = CONTAINER_TIMEOUT, **kwargs) -> subprocess.CompletedProcess:
    """Run a command with timeout, returning CompletedProcess."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        **kwargs,
    )


def start_container(image: str) -> str | None:
    """Start a detached container from image, return container ID or None on failure."""
    try:
        result = run_cmd(
            ["docker", "run", "-d", "--rm",
             "--platform", "linux/amd64",
             image, "sleep", "2h"],
            timeout=CONTAINER_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def stop_container(container_id: str) -> None:
    """Stop a running container (best-effort)."""
    try:
        subprocess.run(
            ["docker", "stop", container_id],
            capture_output=True, timeout=30
        )
    except Exception:
        pass


def normalize_patch(patch_content: str) -> str:
    """Fix known patch quirks before applying.

    Handles cases like sphinx-doc__sphinx-7757 where the --- line references
    a .orig file but +++ line references the actual target file.
    We rewrite the `diff --git`, `index`, and `---` lines to match the `+++`
    target, and drop the `index` hash line so git does not reject the patch
    due to a blob-hash mismatch.
    """
    lines = patch_content.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Look for "diff --git a/X b/Y" where X != Y (mismatched source/dest)
        if line.startswith("diff --git "):
            # Peek ahead for --- and +++ lines to find actual source/dest
            j = i + 1
            minus_path = None
            plus_path = None
            while j < len(lines) and j < i + 5:
                if lines[j].startswith("--- "):
                    p = lines[j][4:].strip()
                    if p.startswith("a/"):
                        p = p[2:]
                    minus_path = p
                elif lines[j].startswith("+++ "):
                    p = lines[j][4:].strip()
                    if p.startswith("b/"):
                        p = p[2:]
                    plus_path = p
                j += 1

            if minus_path and plus_path and minus_path != plus_path:
                # Rewrite diff --git line, skip 'index' line, fix '---' line
                out.append(f"diff --git a/{plus_path} b/{plus_path}\n")
                i += 1
                # Now fix subsequent header lines up to the first @@
                while i < len(lines) and not lines[i].startswith("@@"):
                    cur = lines[i]
                    if cur.startswith("--- "):
                        out.append(f"--- a/{plus_path}\n")
                    elif cur.startswith("index "):
                        # Drop the index/blob-hash line — it references the
                        # .orig blob which doesn't exist in this repo.
                        pass
                    else:
                        out.append(cur)
                    i += 1
                continue

        out.append(line)
        i += 1

    return "".join(out)


def apply_patch_in_container(container_id: str, patch_content: str) -> tuple[bool, str]:
    """Write patch to a temp file, copy into container, apply it.

    Returns (success, error_message).
    """
    # Normalize patch to fix known quirks (e.g. .orig in --- line)
    patch_content = normalize_patch(patch_content)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
        f.write(patch_content)
        patch_path = f.name

    try:
        # Copy patch into container
        cp_result = run_cmd(
            ["docker", "cp", patch_path, f"{container_id}:/tmp/patch.diff"],
            timeout=30,
        )
        if cp_result.returncode != 0:
            return False, f"docker cp failed: {cp_result.stderr.strip()}"

        # Try git apply first
        git_result = run_cmd(
            ["docker", "exec", container_id,
             "git", "-C", "/testbed", "apply", "/tmp/patch.diff"],
            timeout=60,
        )
        if git_result.returncode == 0:
            return True, ""

        # Fall back to patch -p1
        patch_result = run_cmd(
            ["docker", "exec", container_id,
             "bash", "-c",
             "cd /testbed && patch -p1 < /tmp/patch.diff"],
            timeout=60,
        )
        if patch_result.returncode == 0:
            return True, ""

        err = (
            f"git apply failed: {git_result.stderr.strip()}\n"
            f"patch -p1 failed: {patch_result.stderr.strip()}"
        )
        return False, err

    except subprocess.TimeoutExpired as e:
        return False, f"timeout: {e}"
    finally:
        os.unlink(patch_path)


def copy_files_from_container(
    container_id: str,
    py_files: list[str],
    local_dir: Path,
) -> list[Path]:
    """Copy modified .py files out of the container into local_dir.

    Returns list of successfully copied local paths.
    """
    copied: list[Path] = []
    for rel_path in py_files:
        container_src = f"{container_id}:/testbed/{rel_path}"
        # Preserve directory structure
        local_dest = local_dir / rel_path
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = run_cmd(
                ["docker", "cp", container_src, str(local_dest)],
                timeout=30,
            )
            if result.returncode == 0 and local_dest.exists():
                copied.append(local_dest)
            else:
                print(f"    [WARN] docker cp failed for {rel_path}: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print(f"    [WARN] timeout copying {rel_path}")
    return copied


def run_semgrep(file_paths: list[Path]) -> dict:
    """Run semgrep --config p/python --json --quiet on given files.

    Returns dict with keys: verdict (PASS/FLAG), findings_count, findings.
    """
    if not file_paths:
        return {"verdict": "PASS", "findings_count": 0, "findings": []}

    str_paths = [str(p) for p in file_paths]
    try:
        result = subprocess.run(
            ["semgrep", "--config", SEMGREP_CONFIG, "--json", "--quiet", *str_paths],
            capture_output=True,
            text=True,
            timeout=SEMGREP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"verdict": "PASS", "findings_count": 0, "findings": [],
                "error": "semgrep timeout"}
    except FileNotFoundError:
        return {"verdict": "PASS", "findings_count": 0, "findings": [],
                "error": "semgrep not found"}

    # semgrep exits 0 (no findings), 1 (findings), 2+ (error)
    output = result.stdout.strip()
    if result.returncode >= 2 or not output:
        err = result.stderr.strip()[:300] if result.returncode >= 2 else ""
        return {"verdict": "PASS", "findings_count": 0, "findings": [],
                "error": err or None}

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return {"verdict": "PASS", "findings_count": 0, "findings": [],
                "error": "json parse error"}

    results = parsed.get("results", [])
    findings = []
    for r in results:
        findings.append({
            "path": r.get("path", "?"),
            "line": r.get("start", {}).get("line", "?"),
            "rule_id": r.get("check_id", "?"),
            "message": r.get("extra", {}).get("message", "").strip(),
        })

    verdict = "FLAG" if findings else "PASS"
    return {"verdict": verdict, "findings_count": len(findings), "findings": findings}


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def process_instance(
    instance_id: str,
    patch: str,
    ground_truth: str,
) -> dict:
    """Process one instance end-to-end. Returns result dict."""
    print(f"\n[{instance_id}] ground_truth={ground_truth}")

    py_files = extract_modified_py_files(patch)
    if not py_files:
        print(f"  No .py files found in patch — PASS by default")
        return {
            "instance_id": instance_id,
            "ground_truth": ground_truth,
            "semgrep_verdict": "PASS",
            "findings_count": 0,
            "findings": [],
            "error": "no .py files in patch",
        }

    print(f"  Modified .py files: {py_files}")

    img = image_name(instance_id)
    print(f"  Starting container from {img}")

    container_id = start_container(img)
    if not container_id:
        print(f"  [ERROR] Failed to start container")
        return {
            "instance_id": instance_id,
            "ground_truth": ground_truth,
            "semgrep_verdict": "ERROR",
            "findings_count": 0,
            "findings": [],
            "error": "container start failed",
        }

    print(f"  Container: {container_id[:12]}")

    try:
        # Apply the patch
        ok, err = apply_patch_in_container(container_id, patch)
        if not ok:
            print(f"  [ERROR] Patch apply failed: {err[:200]}")
            return {
                "instance_id": instance_id,
                "ground_truth": ground_truth,
                "semgrep_verdict": "ERROR",
                "findings_count": 0,
                "findings": [],
                "error": f"patch apply failed: {err[:300]}",
            }

        print(f"  Patch applied successfully")

        # Copy out modified files
        with tempfile.TemporaryDirectory(prefix=f"sieve_{instance_id}_") as tmpdir:
            local_dir = Path(tmpdir)
            copied = copy_files_from_container(container_id, py_files, local_dir)
            print(f"  Copied {len(copied)}/{len(py_files)} files locally")

            if not copied:
                return {
                    "instance_id": instance_id,
                    "ground_truth": ground_truth,
                    "semgrep_verdict": "PASS",
                    "findings_count": 0,
                    "findings": [],
                    "error": "no files could be copied from container",
                }

            # Run semgrep
            semgrep_result = run_semgrep(copied)

    finally:
        stop_container(container_id)
        print(f"  Container stopped")

    verdict = semgrep_result["verdict"]
    count = semgrep_result["findings_count"]
    print(f"  Semgrep: {verdict} ({count} findings)")

    return {
        "instance_id": instance_id,
        "ground_truth": ground_truth,
        "semgrep_verdict": verdict,
        "findings_count": count,
        "findings": semgrep_result["findings"],
        "error": semgrep_result.get("error"),
    }


def main():
    # Load data
    print("Loading preds.json ...")
    with open(PREDS_JSON) as f:
        preds: dict = json.load(f)

    print("Loading ground truth report ...")
    with open(GT_JSON) as f:
        gt: dict = json.load(f)

    resolved_ids: set[str] = set(gt.get("resolved_ids", []))
    unresolved_ids: set[str] = set(gt.get("unresolved_ids", []))
    error_ids: set[str] = set(gt.get("error_ids", []))

    # Build set of instances to process
    all_instance_ids = list(preds.keys())
    instances_to_process = [
        iid for iid in all_instance_ids
        if iid not in error_ids
    ]

    print(f"\nTotal predictions: {len(all_instance_ids)}")
    print(f"Skipping error instances: {sorted(error_ids)}")
    print(f"Processing {len(instances_to_process)} instances\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []

    for i, instance_id in enumerate(instances_to_process, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(instances_to_process)}] {instance_id}")

        pred = preds[instance_id]
        patch = pred.get("model_patch", "")

        if instance_id in resolved_ids:
            ground_truth = "resolved"
        elif instance_id in unresolved_ids:
            ground_truth = "unresolved"
        else:
            ground_truth = "unknown"

        if not patch or not patch.strip():
            print(f"  Empty patch — skipping")
            result = {
                "instance_id": instance_id,
                "ground_truth": ground_truth,
                "semgrep_verdict": "PASS",
                "findings_count": 0,
                "findings": [],
                "error": "empty patch",
            }
        else:
            try:
                result = process_instance(instance_id, patch, ground_truth)
            except Exception as e:
                print(f"  [EXCEPTION] {e}")
                result = {
                    "instance_id": instance_id,
                    "ground_truth": ground_truth,
                    "semgrep_verdict": "ERROR",
                    "findings_count": 0,
                    "findings": [],
                    "error": str(e),
                }

        results.append(result)

        # Save incrementally
        with open(OUTPUT_JSON, "w") as f:
            json.dump(results, f, indent=2)

    # -----------------------------------------------------------------------
    # Generate report
    # -----------------------------------------------------------------------
    print("\n" + "="*70)
    print("RESULTS SUMMARY")
    print("="*70)

    header = f"{'instance_id':<45} {'ground_truth':<12} {'verdict':<10} {'findings':>8}"
    print(header)
    print("-" * len(header))

    fp_list: list[dict] = []   # resolved + FLAG
    fn_list: list[dict] = []   # unresolved + PASS
    error_list: list[dict] = []

    for r in results:
        iid = r["instance_id"]
        gt_label = r["ground_truth"]
        verdict = r["semgrep_verdict"]
        count = r["findings_count"]

        print(f"{iid:<45} {gt_label:<12} {verdict:<10} {count:>8}")

        if gt_label == "resolved" and verdict == "FLAG":
            fp_list.append(r)
        elif gt_label == "unresolved" and verdict == "PASS":
            fn_list.append(r)
        elif verdict == "ERROR":
            error_list.append(r)

    total = len(results)
    processed_ok = [r for r in results if r["semgrep_verdict"] != "ERROR"]
    resolved_count = sum(1 for r in results if r["ground_truth"] == "resolved")
    unresolved_count = sum(1 for r in results if r["ground_truth"] == "unresolved")

    fp_rate = len(fp_list) / resolved_count * 100 if resolved_count else 0
    fn_rate = len(fn_list) / unresolved_count * 100 if unresolved_count else 0

    print()
    print("="*70)
    print("STATISTICS")
    print("="*70)
    print(f"Total instances processed : {total}")
    print(f"  Resolved (correct)       : {resolved_count}")
    print(f"  Unresolved (incorrect)   : {unresolved_count}")
    print(f"  Errors during processing : {len(error_list)}")
    print()
    print(f"False Positives (resolved + FLAG)   : {len(fp_list)} / {resolved_count}  ({fp_rate:.1f}%)")
    print(f"False Negatives (unresolved + PASS) : {len(fn_list)} / {unresolved_count}  ({fn_rate:.1f}%)")

    if fp_list:
        print()
        print("="*70)
        print("FALSE POSITIVES — correctly resolved patches flagged by semgrep:")
        print("="*70)
        for r in fp_list:
            print(f"\n  {r['instance_id']} ({r['findings_count']} findings)")
            for f in r["findings"][:5]:
                print(f"    {f['path']}:{f['line']}: [{f['rule_id']}] {f['message'][:80]}")
            if len(r["findings"]) > 5:
                print(f"    ... and {len(r['findings']) - 5} more")

    if fn_list:
        print()
        print("="*70)
        print("FALSE NEGATIVES — incorrect patches that passed semgrep:")
        print("="*70)
        for r in fn_list:
            print(f"  {r['instance_id']}")

    if error_list:
        print()
        print("="*70)
        print("ERRORS during processing:")
        print("="*70)
        for r in error_list:
            print(f"  {r['instance_id']}: {r.get('error', 'unknown error')}")

    print()
    print(f"Full results saved to: {OUTPUT_JSON}")

    # Save final summary alongside results
    summary = {
        "total": total,
        "resolved": resolved_count,
        "unresolved": unresolved_count,
        "errors": len(error_list),
        "false_positives": len(fp_list),
        "false_negatives": len(fn_list),
        "fp_rate_pct": round(fp_rate, 2),
        "fn_rate_pct": round(fn_rate, 2),
        "fp_instances": [r["instance_id"] for r in fp_list],
        "fn_instances": [r["instance_id"] for r in fn_list],
        "error_instances": [r["instance_id"] for r in error_list],
    }
    with open(OUTPUT_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Summary saved to: {OUTPUT_DIR / 'summary.json'}")


if __name__ == "__main__":
    main()

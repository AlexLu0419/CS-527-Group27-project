#!/usr/bin/env python3
"""validate_dynamic_checks.py

Runs the Layer-2 regression test checker against the 50-instance Gemini 2.5 Pro
pilot set and reports a confusion matrix.

Checker evaluated
-----------------
  check_regression — run related project tests before/after the patch; report
                     tests that newly fail after the patch is applied.

  For each instance a fresh SWE-bench Docker container is started so the
  repository is in exactly the pre-patch state.  Test discovery uses the same
  naming-convention heuristic as ``sieve/phases/dynamic.py``:

    Django  (tests/runtests.py present):
      ``django/X/Y.py`` → try ``tests/X/test_Y.py`` (module X.test_Y),
      then directories in ``tests/`` whose name starts with ``X_`` or equals
      ``X`` (covers the admin_views / admin_checks pattern).

    Sphinx / generic pytest:
      ``pkg/sub/Y.py`` → try ``tests/test_Y.py``, ``tests/test_sub.py``, …

  Both runners are invoked via ``bash -lc`` (login shell) so the conda
  ``testbed`` environment is automatically activated.

Delta approach
--------------
  Tests are run BEFORE and AFTER applying the patch.  Only tests that were
  *passing* before and *fail* after count as regressions.  Pre-existing test
  failures are subtracted, preventing false positives in files that already
  have a broken test.

Output
------
  runs/dynamic_checks_validation/results.json   per-instance verdicts
  runs/dynamic_checks_validation/summary.json   aggregated confusion matrix
  stdout                                        progress + final report

Usage
-----
  python scripts/validate_dynamic_checks.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
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
OUTPUT_DIR  = REPO_ROOT / "runs/dynamic_checks_validation"
OUTPUT_JSON = OUTPUT_DIR / "results.json"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"

TEST_TIMEOUT = 120   # seconds per test run (before or after)

# ---------------------------------------------------------------------------
# Docker helpers  (mirrors validate_static_checks.py)
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


def docker_exec_login(cid: str, shell_cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run shell_cmd inside container via bash login shell (activates conda testbed env)."""
    return run_cmd(["docker", "exec", cid, "bash", "-lc", shell_cmd], timeout=timeout)


def docker_exec(cid: str, cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return run_cmd(["docker", "exec", cid, *cmd], timeout=timeout)


def copy_patch_to_container(cid: str, patch_content: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
        f.write(patch_content)
        local = f.name
    try:
        r = run_cmd(["docker", "cp", local, f"{cid}:/tmp/patch.diff"], timeout=30)
        return (r.returncode == 0, r.stderr.strip())
    finally:
        os.unlink(local)


def apply_patch(cid: str) -> tuple[bool, str]:
    r = docker_exec(cid, ["git", "-C", "/testbed", "apply", "/tmp/patch.diff"])
    if r.returncode == 0:
        return True, ""
    # fallback
    r2 = docker_exec_login(cid, "cd /testbed && patch -p1 < /tmp/patch.diff")
    if r2.returncode == 0:
        return True, ""
    err = f"git apply: {r.stderr.strip()[:200]}\npatch: {r2.stderr.strip()[:200]}"
    return False, err

# ---------------------------------------------------------------------------
# Patch parsing  (mirrors sieve/phases/dynamic.py)
# ---------------------------------------------------------------------------

def extract_changed_source_files(patch: str) -> list[str]:
    """Return .py source files changed by the patch (test files excluded)."""
    files: list[str] = []
    seen: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[4:].strip().split("\t")[0]
        if path.startswith("b/"):
            path = path[2:]
        if not path or path.startswith("/dev/") or not path.endswith(".py"):
            continue
        p = Path(path)
        if p.name.startswith("test_") or "/tests/" in path:
            continue
        if path not in seen:
            seen.add(path)
            files.append(path)
    return files

# ---------------------------------------------------------------------------
# Test runner detection  (inside container)
# ---------------------------------------------------------------------------

def detect_test_runner(cid: str) -> str:
    r = docker_exec(cid, ["test", "-f", "/testbed/tests/runtests.py"])
    return "django" if r.returncode == 0 else "pytest"

# ---------------------------------------------------------------------------
# Test discovery  (based on sieve/phases/dynamic.py heuristics)
# ---------------------------------------------------------------------------

def find_related_tests_django(cid: str, source_files: list[str]) -> list[str]:
    """Return runtests.py module names related to the changed source files."""
    modules: list[str] = []
    seen: set[str] = set()

    for src in source_files:
        parts = list(Path(src).parts)
        while parts and parts[0] in ("django", "contrib"):
            parts.pop(0)
        if not parts:
            continue

        stem   = Path(parts[-1]).stem
        parent = parts[-2] if len(parts) >= 2 else ""

        # Candidate 1: exact nested file
        if parent:
            mod      = f"{parent}.test_{stem}"
            test_rel = f"tests/{parent}/test_{stem}.py"
            r = docker_exec(cid, ["test", "-f", f"/testbed/{test_rel}"])
            if r.returncode == 0 and mod not in seen:
                seen.add(mod)
                modules.append(mod)
                continue

        # Candidate 2: directories in tests/ matching the parent name prefix
        if parent:
            r = docker_exec_login(
                cid,
                f"ls /testbed/tests/ | grep -E '^{re.escape(parent)}(_|$|s$)' 2>/dev/null || true",
                timeout=10,
            )
            prefix_dirs = [d for d in r.stdout.strip().splitlines() if d]
            for dirname in prefix_dirs:
                if dirname not in seen:
                    seen.add(dirname)
                    modules.append(dirname)
            if prefix_dirs:
                continue

        # Candidate 3: top-level test_<stem>.py
        r = docker_exec(cid, ["test", "-f", f"/testbed/tests/test_{stem}.py"])
        if r.returncode == 0:
            mod = f"test_{stem}"
            if mod not in seen:
                seen.add(mod)
                modules.append(mod)
            continue

        # Candidate 4: any test_<stem>.py anywhere under tests/
        r = docker_exec_login(
            cid,
            f"find /testbed/tests -name 'test_{re.escape(stem)}.py' 2>/dev/null | head -1",
            timeout=15,
        )
        hit = r.stdout.strip()
        if hit:
            # convert to module name
            rel = hit.replace("/testbed/tests/", "").replace("/", ".").removesuffix(".py")
            if rel not in seen:
                seen.add(rel)
                modules.append(rel)

    return modules


def find_related_tests_pytest(cid: str, source_files: list[str]) -> list[str]:
    """Return pytest file paths related to the changed source files."""
    found: list[str] = []
    seen: set[str] = set()

    for src in source_files:
        p      = Path(src)
        stem   = p.stem
        parent = p.parent.name

        candidates = [
            f"tests/test_{stem}.py",
            f"tests/test_{parent}.py",
            f"tests/test_{parent}_{stem}.py",
            f"tests/{parent}/test_{stem}.py",
            f"tests/test_{parent}s.py",
        ]
        for c in candidates:
            if c in seen:
                break
            r = docker_exec(cid, ["test", "-f", f"/testbed/{c}"])
            if r.returncode == 0:
                seen.add(c)
                found.append(c)
                break

    return found

# ---------------------------------------------------------------------------
# Test execution inside container
# ---------------------------------------------------------------------------

def parse_django_failures(output: str) -> set[str]:
    failures: set[str] = set()
    for line in output.splitlines():
        m = re.match(r"^(FAIL|ERROR):\s+(\S+)\s+\((.+)\)", line)
        if m:
            failures.add(f"{m.group(3)}.{m.group(2)}")
    return failures


def parse_pytest_failures(output: str) -> set[str]:
    failures: set[str] = set()
    for line in output.splitlines():
        m = re.match(r"^FAILED\s+(\S+)", line)
        if m:
            failures.add(m.group(1))
        m2 = re.match(r"^(\S+::.*)\s+FAILED\s*$", line)
        if m2:
            failures.add(m2.group(1))
    return failures


def run_django_tests(cid: str, modules: list[str]) -> dict:
    if not modules:
        return {"failed": set(), "output": "", "returncode": 0}
    mods_str = " ".join(modules)
    cmd = f"cd /testbed && python tests/runtests.py {mods_str} --verbosity=1 --parallel=1 2>&1"
    r = docker_exec_login(cid, cmd, timeout=TEST_TIMEOUT)
    output = r.stdout
    return {
        "failed": parse_django_failures(output),
        "output": output,
        "returncode": r.returncode,
    }


def run_pytest_tests(cid: str, test_files: list[str]) -> dict:
    if not test_files:
        return {"failed": set(), "output": "", "returncode": 0}
    files_str = " ".join(test_files)
    cmd = f"cd /testbed && python -m pytest {files_str} -v --tb=no -q 2>&1"
    r = docker_exec_login(cid, cmd, timeout=TEST_TIMEOUT)
    output = r.stdout
    return {
        "failed": parse_pytest_failures(output),
        "output": output,
        "returncode": r.returncode,
    }

# ---------------------------------------------------------------------------
# Per-instance processing
# ---------------------------------------------------------------------------

def process_instance(instance_id: str, patch: str, ground_truth: str) -> dict:
    source_files = extract_changed_source_files(patch)

    result: dict = {
        "instance_id":    instance_id,
        "ground_truth":   ground_truth,
        "source_files":   source_files,
        "test_targets":   [],
        "test_runner":    "unknown",
        "pre_failed":     [],
        "post_failed":    [],
        "new_failures":   [],
        "verdict":        "ERROR",
        "message":        "",
        "error":          None,
    }

    img = image_name(instance_id)
    cid = start_container(img)
    if cid is None:
        result["error"]   = "container start failed"
        result["message"] = "container start failed"
        return result

    print(f"  Container {cid[:12]} started")

    try:
        ok, err = copy_patch_to_container(cid, patch)
        if not ok:
            result["error"]   = f"docker cp failed: {err}"
            result["message"] = result["error"]
            return result

        runner = detect_test_runner(cid)
        result["test_runner"] = runner

        # Discover test targets
        if runner == "django":
            targets = find_related_tests_django(cid, source_files)
        else:
            targets = find_related_tests_pytest(cid, source_files)

        result["test_targets"] = targets
        print(f"  runner={runner}  targets={targets or '(none)'}")

        if not targets:
            result["verdict"] = "SKIP"
            result["message"] = "no related tests found"
            return result

        # Run before
        if runner == "django":
            pre  = run_django_tests(cid, targets)
            post_fn = lambda: run_django_tests(cid, targets)
        else:
            pre  = run_pytest_tests(cid, targets)
            post_fn = lambda: run_pytest_tests(cid, targets)

        pre_failed = pre["failed"]
        result["pre_failed"] = sorted(pre_failed)
        print(f"  pre-patch:  {len(pre_failed)} failing")

        # Apply patch
        ok, err = apply_patch(cid)
        if not ok:
            result["verdict"] = "ERROR"
            result["error"]   = f"patch apply failed: {err}"
            result["message"] = result["error"]
            return result

        # Run after
        post       = post_fn()
        post_failed = post["failed"]
        result["post_failed"] = sorted(post_failed)
        print(f"  post-patch: {len(post_failed)} failing")

        # Delta
        new_failures = sorted(post_failed - pre_failed)
        result["new_failures"] = new_failures

        if new_failures:
            result["verdict"] = "REJECT"
            result["message"] = f"{len(new_failures)} new regression(s): " + "; ".join(new_failures[:3])
            print(f"  REJECT — {len(new_failures)} new failure(s)")
            for t in new_failures[:5]:
                print(f"    {t}")
        else:
            result["verdict"] = "PASS"
            result["message"] = (
                f"{len(pre_failed)} pre-existing, 0 new failures in {targets}"
            )
            print(f"  PASS")

    except Exception as e:
        result["error"]   = str(e)
        result["message"] = str(e)
        result["verdict"] = "ERROR"
        print(f"  [EXCEPTION] {e}")
    finally:
        stop_container(cid)
        print("  Container stopped")

    return result

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def confusion_stats(results: list[dict]) -> dict:
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
        if gt == "resolved":     # correct patch
            if v == "REJECT":
                fp += 1          # false positive
            else:
                tn += 1          # true negative: correctly passed
        else:                    # unresolved / incorrect patch
            if v == "REJECT":
                tp += 1          # true positive: caught bad patch
            else:
                fn += 1          # false negative: bad patch slipped through
    return dict(tp=tp, fp=fp, tn=tn, fn=fn, skip=skip, err=err)


def print_confusion(stats: dict, resolved_n: int, unresolved_n: int) -> None:
    tp, fp, tn, fn = stats["tp"], stats["fp"], stats["tn"], stats["fn"]
    print(f"\n{'─'*60}")
    print(f"  check_regression")
    print(f"{'─'*60}")
    print(f"  Ground-truth resolved   ({resolved_n:2d} patches):")
    print(f"    PASS (correct) : {tn:2d}  TN")
    print(f"    REJECT         : {fp:2d}  ← false positive")
    print(f"  Ground-truth unresolved ({unresolved_n:2d} patches):")
    print(f"    REJECT (caught): {tp:2d}  TP")
    print(f"    PASS (missed)  : {fn:2d}  FN")
    if stats["skip"] or stats["err"]:
        print(f"  SKIP / ERROR   : {stats['skip']} / {stats['err']}")
    total_bad  = tp + fn
    total_good = tn + fp
    if total_bad:
        print(f"  Catch rate : {tp}/{total_bad} ({tp/total_bad*100:.0f}%)")
    if total_good:
        print(f"  Hard FP rate: {fp}/{total_good} ({fp/total_good*100:.0f}%)")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Loading preds.json …")
    preds: dict = json.loads(PREDS_JSON.read_text())

    print("Loading ground-truth report …")
    gt: dict = json.loads(GT_JSON.read_text())

    resolved_ids   = set(gt.get("resolved_ids",   []))
    unresolved_ids = set(gt.get("unresolved_ids", []))
    error_ids      = set(gt.get("error_ids",       []))

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
                "source_files": [], "test_targets": [], "test_runner": "unknown",
                "pre_failed": [], "post_failed": [], "new_failures": [],
                "verdict": "SKIP", "message": "empty patch", "error": "empty patch",
            })
        else:
            try:
                r = process_instance(iid, patch, gt_label)
            except Exception as e:
                print(f"  [EXCEPTION] {e}")
                r = {
                    "instance_id": iid, "ground_truth": gt_label,
                    "source_files": [], "test_targets": [], "test_runner": "unknown",
                    "pre_failed": [], "post_failed": [], "new_failures": [],
                    "verdict": "ERROR", "message": str(e), "error": str(e),
                }
            results.append(r)

        OUTPUT_JSON.write_text(json.dumps(results, indent=2))

    # Final report
    resolved_results   = [r for r in results if r["ground_truth"] == "resolved"]
    unresolved_results = [r for r in results if r["ground_truth"] == "unresolved"]
    rn = len(resolved_results)
    un = len(unresolved_results)

    print("\n\n" + "="*60)
    print("RESULTS TABLE")
    print("="*60)
    header = f"{'instance_id':<45} {'gt':<12} {'runner':<8} {'targets':<5} {'verdict':<8}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['instance_id']:<45} {r['ground_truth']:<12} "
            f"{r.get('test_runner','?'):<8} {len(r.get('test_targets',[])):<5} "
            f"{r['verdict']:<8}"
            + (f"  — {r['message'][:60]}" if r["verdict"] not in ("PASS", "SKIP") else "")
        )

    print("\n\n" + "="*60)
    print("CONFUSION MATRIX")
    print("="*60)
    stats = confusion_stats(results)
    print_confusion(stats, rn, un)

    print(f"\n{'='*60}")
    print("FALSE POSITIVES  (REJECT on a correctly-resolved patch)")
    print(f"{'='*60}")
    fps = [r for r in resolved_results if r["verdict"] == "REJECT"]
    if fps:
        for r in fps:
            print(f"  {r['instance_id']} — {r['message'][:100]}")
    else:
        print("  None.")

    print(f"\n{'='*60}")
    print("TRUE POSITIVES  (REJECT on an unresolved patch)")
    print(f"{'='*60}")
    tps = [r for r in unresolved_results if r["verdict"] == "REJECT"]
    if tps:
        for r in tps:
            print(f"  {r['instance_id']} — {r['message'][:100]}")
    else:
        print("  None.")

    print(f"\n{'='*60}")
    print("INSTANCES WITH NO RELATED TESTS FOUND")
    print(f"{'='*60}")
    skips = [r for r in results if r["verdict"] == "SKIP"]
    for r in skips:
        print(f"  {r['instance_id']} ({r['ground_truth']}) — {r['source_files']}")

    summary = {
        "total_processed":  len(results),
        "resolved":         rn,
        "unresolved":       un,
        "check_regression": stats,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2))

    print(f"\nFull results : {OUTPUT_JSON}")
    print(f"Summary      : {SUMMARY_JSON}")


if __name__ == "__main__":
    main()

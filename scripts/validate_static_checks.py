#!/usr/bin/env python3
"""validate_static_checks.py

Runs the three Layer-1 static checkers independently against the 50-instance
Gemini 2.5 Pro pilot set and reports per-checker confusion matrices.

Checkers evaluated
------------------
1. check_patch_applies  — git apply --check (dry-run, no disk changes)
2. check_files_parse    — ast.parse + py_compile on every modified .py file
3. check_lint_delta     — flake8 --select=F,E9 before/after delta

Each checker is executed via Docker exec inside the per-instance SWE-bench
container so the repo is in exactly the state expected by the pipeline.

For checker 2 and 3 the patch must be applied first, so the script does:
  apply patch  →  check_files_parse (reads disk)
  apply patch  →  [already done]  →  check_lint_delta (uses the diff)

Since check_lint_delta applies the patch internally we run check_files_parse
AFTER check_lint_delta to avoid double-apply.  The container is fresh per
instance so the ordering is clean.

Usage
-----
    python scripts/validate_static_checks.py

Output
------
    runs/static_checks_validation/results.json   per-instance verdicts
    runs/static_checks_validation/summary.json   aggregated confusion matrices
    stdout                                        progress + final report
"""

from __future__ import annotations

import json
import os
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
OUTPUT_DIR = REPO_ROOT / "runs/static_checks_validation"
OUTPUT_JSON = OUTPUT_DIR / "results.json"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"

# ---------------------------------------------------------------------------
# Flake8 codes — mirrors sieve/phases/static.py
# ---------------------------------------------------------------------------
REJECT_CODES = {
    "F404", "F821", "F822", "F823",
    "F701", "F702", "F704", "F706", "F707",
    "F502", "F503", "F505", "F507", "F508", "F509",
    "F524", "F621", "F622", "F831", "F901",
}
FLAG_CODES = {
    "F401", "F811", "F841", "F842", "F405",
    "F631", "F634", "F632", "F601", "F602",
    "F501", "F504", "F521", "F522", "F523", "F525",
    "F541", "F542", "F824",
}
IGNORE_CODES = {"E501", "F402", "F403", "F406", "F407", "F721", "F722", "F723", "F633"}


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


def _install_flake8(cid: str) -> None:
    """Pip-install flake8 + flake8-json inside the container (idempotent)."""
    docker_exec_sh(
        cid,
        "pip install flake8 flake8-json --quiet --disable-pip-version-check 2>&1 || true",
        timeout=120,
    )


def docker_exec(cid: str, cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return run_cmd(["docker", "exec", cid, *cmd], timeout=timeout)


def docker_exec_sh(cid: str, shell_cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return run_cmd(["docker", "exec", cid, "bash", "-c", shell_cmd], timeout=timeout)


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
# Individual checker implementations (run via docker exec)
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


def apply_patch(cid: str) -> tuple[bool, str]:
    """Apply the patch (git apply, fallback patch -p1). Returns (ok, error)."""
    r = docker_exec(cid, ["git", "-C", "/testbed", "apply", "/tmp/patch.diff"])
    if r.returncode == 0:
        return True, ""
    r2 = docker_exec_sh(cid, "cd /testbed && patch -p1 < /tmp/patch.diff")
    if r2.returncode == 0:
        return True, ""
    err = f"git apply: {r.stderr.strip()[:200]}\npatch -p1: {r2.stderr.strip()[:200]}"
    return False, err


def check_files_parse(cid: str, py_files: list[str]) -> dict:
    """
    Verdict: REJECT on first .py file that fails ast.parse or py_compile.
    Assumes patch has already been applied.
    """
    for rel in py_files:
        path = f"/testbed/{rel}"
        # Check if file still exists (might have been deleted by patch)
        exists = docker_exec_sh(cid, f"test -f {path}")
        if exists.returncode != 0:
            continue  # deleted file — skip

        # ast.parse via python3 one-liner
        py_cmd = (
            f"python3 -c \""
            f"import ast, sys; "
            f"src = open('{path}', encoding='utf-8', errors='replace').read(); "
            f"ast.parse(src, filename='{path}')"
            f"\""
        )
        r = docker_exec_sh(cid, py_cmd)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()[:300]
            return {"verdict": "REJECT", "message": f"{rel}: {err}"}

        # py_compile as secondary check
        r2 = docker_exec_sh(cid, f"python3 -m py_compile {path} 2>&1")
        if r2.returncode != 0:
            err = r2.stdout.strip()[:300]
            return {"verdict": "REJECT", "message": f"{rel}: {err}"}

    return {"verdict": "PASS", "message": ""}


def _parse_flake8_json(raw: str) -> set[tuple[str, int, int, str]]:
    """Parse flake8 --format=json output into a set of (code, line, col, text)."""
    findings: set[tuple[str, int, int, str]] = set()
    if not raw.strip():
        return findings
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return findings
    for _filename, diags in parsed.items():
        for d in diags:
            code = str(d.get("code", "")).strip()
            findings.add((
                code,
                int(d.get("line_number", 0)),
                int(d.get("column_number", 0)),
                str(d.get("text", "")).strip(),
            ))
    return findings


def _classify_new_findings(
    new: list[tuple[str, int, int, str]],
) -> tuple[list, list]:
    """Split new findings into (reject_list, flag_list)."""
    reject, flag = [], []
    for f in new:
        code = f[0]
        if code in IGNORE_CODES:
            continue
        if code.startswith("E9") or code in REJECT_CODES:
            reject.append(f)
        elif code in FLAG_CODES or code.startswith("F"):
            flag.append(f)
    return reject, flag


def check_lint_delta(cid: str, py_files: list[str]) -> dict:
    """
    Run flake8 before and after applying the patch.
    Verdict: REJECT (new E9/reject-code findings), FLAG (new flag-code findings), or PASS.
    NOTE: This function applies the patch internally.
    """
    if not py_files:
        return {"verdict": "PASS", "message": "no .py files"}

    files_arg = " ".join(f"/testbed/{f}" for f in py_files)
    flake8_cmd = f"cd /testbed && flake8 --select=F,E9 --format=json {files_arg} 2>/dev/null || true"

    # --- before ---
    r_before = docker_exec_sh(cid, flake8_cmd, timeout=60)
    before = _parse_flake8_json(r_before.stdout)

    # --- apply patch ---
    ok, err = apply_patch(cid)
    if not ok:
        return {"verdict": "REJECT", "message": f"patch apply failed: {err}"}

    # --- after ---
    r_after = docker_exec_sh(cid, flake8_cmd, timeout=60)
    after = _parse_flake8_json(r_after.stdout)

    new_findings = sorted(after - before)
    reject, flag = _classify_new_findings(new_findings)

    def fmt(findings):
        return "; ".join(f"line {ln} col {col}: {code} {txt}"
                         for code, ln, col, txt in findings[:5])

    if reject:
        return {"verdict": "REJECT", "message": fmt(reject)}
    if flag:
        return {"verdict": "FLAG", "message": fmt(flag)}
    return {"verdict": "PASS", "message": ""}


# ---------------------------------------------------------------------------
# Per-instance processing
# ---------------------------------------------------------------------------

def process_instance(instance_id: str, patch: str, ground_truth: str) -> dict:
    """Run all three checkers on one instance. Returns result dict."""
    py_files = extract_modified_py_files(patch)

    result: dict = {
        "instance_id": instance_id,
        "ground_truth": ground_truth,
        "modified_py_files": py_files,
        "check_patch_applies": {"verdict": "SKIP", "message": ""},
        "check_files_parse":   {"verdict": "SKIP", "message": ""},
        "check_lint_delta":    {"verdict": "SKIP", "message": ""},
        "error": None,
    }

    img = image_name(instance_id)
    cid = start_container(img)
    if cid is None:
        result["error"] = "container start failed"
        for k in ("check_patch_applies", "check_files_parse", "check_lint_delta"):
            result[k] = {"verdict": "ERROR", "message": "container start failed"}
        return result

    print(f"  Container {cid[:12]} started")
    try:
        # Copy patch into container
        ok, err = copy_patch_to_container(cid, patch)
        if not ok:
            result["error"] = f"docker cp patch failed: {err}"
            for k in ("check_patch_applies", "check_files_parse", "check_lint_delta"):
                result[k] = {"verdict": "ERROR", "message": result["error"]}
            return result

        # ---- 0. Ensure flake8 is available inside the container ----
        _install_flake8(cid)

        # ---- 1. check_patch_applies (dry-run, patch NOT applied yet) ----
        cpa = check_patch_applies(cid)
        result["check_patch_applies"] = cpa
        print(f"  check_patch_applies: {cpa['verdict']}"
              + (f" — {cpa['message'][:80]}" if cpa["verdict"] == "REJECT" else ""))

        if cpa["verdict"] == "REJECT":
            # No point running subsequent checks on a non-applicable patch.
            result["check_files_parse"]  = {"verdict": "SKIP", "message": "patch did not apply"}
            result["check_lint_delta"]   = {"verdict": "SKIP", "message": "patch did not apply"}
            return result

        # ---- 2. check_lint_delta (applies patch internally) ----
        # Must run before check_files_parse because it applies the patch.
        if not py_files:
            result["check_lint_delta"] = {"verdict": "PASS", "message": "no .py files"}
        else:
            cld = check_lint_delta(cid, py_files)
            result["check_lint_delta"] = cld
            print(f"  check_lint_delta:    {cld['verdict']}"
                  + (f" — {cld['message'][:80]}" if cld["verdict"] != "PASS" else ""))

        # ---- 3. check_files_parse (patch already applied above) ----
        if not py_files:
            result["check_files_parse"] = {"verdict": "PASS", "message": "no .py files"}
        else:
            cfp = check_files_parse(cid, py_files)
            result["check_files_parse"] = cfp
            print(f"  check_files_parse:   {cfp['verdict']}"
                  + (f" — {cfp['message'][:80]}" if cfp["verdict"] == "REJECT" else ""))

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

VERDICT_ORDER = ("REJECT", "FLAG", "PASS", "SKIP", "ERROR")


def confusion_stats(results: list[dict], checker: str) -> dict:
    """Compute TP/FP/TN/FN/FLAG counts for one checker."""
    tp = fp = tn = fn = flag_good = flag_bad = skip = err = 0
    for r in results:
        gt = r["ground_truth"]
        v = r[checker]["verdict"]
        if v == "SKIP":
            skip += 1
            continue
        if v == "ERROR":
            err += 1
            continue
        if gt == "resolved":       # correct patch
            if v == "REJECT":
                fp += 1            # false positive: rejected a good patch
            elif v == "FLAG":
                flag_good += 1     # flag on a good patch (soft FP)
            else:
                tn += 1            # true negative: correctly passed
        else:                      # unresolved / incorrect patch
            if v == "REJECT":
                tp += 1            # true positive: correctly rejected a bad patch
            elif v == "FLAG":
                flag_bad += 1      # flag on a bad patch (soft TP)
            else:
                fn += 1            # false negative: bad patch slipped through
    return dict(tp=tp, fp=fp, tn=tn, fn=fn,
                flag_good=flag_good, flag_bad=flag_bad,
                skip=skip, err=err)


def print_confusion(checker: str, stats: dict, resolved_n: int, unresolved_n: int) -> None:
    tp, fp, tn, fn = stats["tp"], stats["fp"], stats["tn"], stats["fn"]
    fg, fb = stats["flag_good"], stats["flag_bad"]

    print(f"\n{'─'*60}")
    print(f"  {checker}")
    print(f"{'─'*60}")
    print(f"  Ground-truth resolved   ({resolved_n:2d} patches):")
    print(f"    PASS (correct)  : {tn:2d}")
    print(f"    FLAG            : {fg:2d}  ← soft FP")
    print(f"    REJECT          : {fp:2d}  ← false positive")
    print(f"  Ground-truth unresolved ({unresolved_n:2d} patches):")
    print(f"    REJECT (caught) : {tp:2d}  ← true positive")
    print(f"    FLAG            : {fb:2d}  ← soft TP")
    print(f"    PASS (missed)   : {fn:2d}  ← false negative")
    if stats["skip"] or stats["err"]:
        print(f"  SKIP / ERROR    : {stats['skip']} / {stats['err']}")

    total_bad = tp + fb + fn
    if total_bad:
        catch = (tp + fb) / total_bad * 100
        print(f"  Catch rate (bad patches caught by REJECT+FLAG): {tp + fb}/{total_bad} ({catch:.0f}%)")
    total_good = tn + fg + fp
    if total_good:
        fpr = fp / total_good * 100
        print(f"  Hard FP rate   (REJECT on good patch):          {fp}/{total_good} ({fpr:.0f}%)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
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
                "check_files_parse":   {"verdict": "PASS", "message": "empty patch"},
                "check_lint_delta":    {"verdict": "PASS", "message": "empty patch"},
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
                    "check_files_parse":   {"verdict": "ERROR", "message": str(e)},
                    "check_lint_delta":    {"verdict": "ERROR", "message": str(e)},
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

    CHECKERS = ["check_patch_applies", "check_files_parse", "check_lint_delta"]

    print("\n\n" + "="*60)
    print("RESULTS TABLE")
    print("="*60)
    header = f"{'instance_id':<45} {'gt':<12} {'applies':<9} {'parse':<9} {'lint':<9}"
    print(header)
    print("-" * len(header))
    for r in results:
        iid  = r["instance_id"]
        gt   = r["ground_truth"]
        v1   = r["check_patch_applies"]["verdict"]
        v2   = r["check_files_parse"]["verdict"]
        v3   = r["check_lint_delta"]["verdict"]
        print(f"{iid:<45} {gt:<12} {v1:<9} {v2:<9} {v3:<9}")

    print("\n\n" + "="*60)
    print("PER-CHECKER CONFUSION MATRICES")
    print("="*60)

    all_stats = {}
    for checker in CHECKERS:
        stats = confusion_stats(results, checker)
        all_stats[checker] = stats
        print_confusion(checker, stats, rn, un)

    # Combined: what does each checker uniquely catch?
    print(f"\n\n{'='*60}")
    print("UNIQUE CATCHES  (bad patches caught only by this checker)")
    print(f"{'='*60}")
    for checker in CHECKERS:
        # A "catch" = REJECT or FLAG on an unresolved patch
        caught_by = {
            r["instance_id"]
            for r in unresolved_results
            if r[checker]["verdict"] in ("REJECT", "FLAG")
        }
        # Unique = caught by this checker but not caught by any other checker
        other_caught = set()
        for other in CHECKERS:
            if other != checker:
                other_caught |= {
                    r["instance_id"]
                    for r in unresolved_results
                    if r[other]["verdict"] in ("REJECT", "FLAG")
                }
        unique = caught_by - other_caught
        print(f"  {checker:<30}: total={len(caught_by):2d}  unique={len(unique):2d}")
        for iid in sorted(unique):
            v = next(r[checker]["verdict"] for r in unresolved_results if r["instance_id"] == iid)
            print(f"      {iid}  [{v}]")

    # False positives (REJECT on resolved)
    print(f"\n{'='*60}")
    print("FALSE POSITIVES  (REJECT on a correctly-resolved patch)")
    print(f"{'='*60}")
    any_fp = False
    for checker in CHECKERS:
        fps = [r for r in resolved_results if r[checker]["verdict"] == "REJECT"]
        if fps:
            any_fp = True
            for r in fps:
                print(f"  {checker}: {r['instance_id']} — {r[checker]['message'][:100]}")
    if not any_fp:
        print("  None.")

    # Save summary
    summary = {
        "total_processed": len(results),
        "resolved": rn,
        "unresolved": un,
        "per_checker": {c: all_stats[c] for c in CHECKERS},
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2))

    print(f"\nFull results : {OUTPUT_JSON}")
    print(f"Summary      : {SUMMARY_JSON}")


if __name__ == "__main__":
    main()

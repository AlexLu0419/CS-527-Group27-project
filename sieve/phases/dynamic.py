"""SIEVE Dynamic Checker — Layer 2 of the evaluation cascade.

Implemented:
    Phase 2.1 — Regression Test Check
        check_regression_tests  — run PASS_TO_PASS tests before/after patch (delta approach)
                                   Requires: SWE-bench Docker image for the instance

    Phase 2.2 — Reproduction Test Check
        check_reproduction_test — run a repro script on patched code; must PASS
                                   Currently skipped when repro_test_code is None

    Phase 2.3 — Orchestration
        run_dynamic_checks      — starts container, runs both checks, returns DynamicCheckResult
        DynamicCheckResult      — aggregate dataclass (verdict + per-check details)

Design notes
------------
- The ``PASS_TO_PASS`` field from the SWE-bench Verified dataset is the authoritative
  source of regression tests.  It is a JSON-encoded list of pytest node IDs stored in
  the instance dict.  This is more reliable than heuristic test-file discovery.

- ``check_regression_tests`` owns the single patch application: it runs tests on the
  unpatched container, applies the patch, then runs tests again.  The delta (newly
  failing tests) drives the verdict.

- ``check_reproduction_test`` runs the repro script on the already-patched container.
  The patch is always applied by ``check_regression_tests`` first.

- If no ``repro_test_code`` is provided the reproduction check is recorded as SKIP
  (non-blocking).

Verdict semantics (same as Layer 1)
------------------------------------
  PASS   — check passed cleanly
  REJECT — check found a problem; overall verdict becomes REJECT
  FLAG   — warning, non-blocking
  SKIP   — check not applicable (no tests / no repro script)
  ERROR  — infrastructure failure (container, patch apply, etc.)
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("sieve.phases.dynamic")


# ---------------------------------------------------------------------------
# Per-check result type  (mirrors sieve/phases/static.py)
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    """Result of a single dynamic check."""

    verdict: str       # "PASS" | "REJECT" | "FLAG" | "SKIP" | "ERROR"
    check_name: str
    message: str = ""  # Human-readable detail


@dataclass
class DynamicCheckResult:
    """Aggregate result returned by ``run_dynamic_checks``.

    Fields
    ------
    verdict : str
        Overall verdict — ``"REJECT"`` if any check rejected the patch,
        ``"FLAG"`` if any check flagged concerns (none rejected),
        ``"SKIP"`` if all applicable checks were skipped (no tests available),
        ``"PASS"`` if all checks passed cleanly.
        ``"ERROR"`` if a container or infrastructure failure prevented evaluation.
    checks_passed : list[str]
        Names of checks that returned PASS.
    checks_failed : list[str]
        Names of checks that returned REJECT.
    flags : list[str]
        Human-readable warning messages from FLAG checks (non-blocking).
    details : dict[str, CheckResult]
        Full ``CheckResult`` for every check, keyed by ``check_name``.
    """

    verdict: str
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    details: dict[str, CheckResult] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize to a plain dict (suitable for JSON serialisation)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Docker helpers  (ported from scripts/validate_dynamic_checks.py)
# ---------------------------------------------------------------------------

def _image_name(instance_id: str) -> str:
    """Return the SWE-bench Docker image name for an instance."""
    transformed = instance_id.replace("__", "_1776_").lower()
    return f"swebench/sweb.eval.x86_64.{transformed}:latest"


def _run_cmd(
    cmd: list[str],
    timeout: int = 120,
    **kwargs,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        **kwargs,
    )


def _start_container(img: str) -> str | None:
    """Start a detached Docker container; return its ID, or None on failure."""
    try:
        r = _run_cmd(
            ["docker", "run", "-d", "--rm", "--platform", "linux/amd64", img, "sleep", "2h"],
            timeout=180,
        )
        cid = r.stdout.strip()
        if r.returncode == 0 and cid:
            return cid
        logger.warning("_start_container: failed for %s — %s", img, r.stderr.strip()[:200])
        return None
    except Exception as exc:
        logger.warning("_start_container: exception — %s", exc)
        return None


def _stop_container(cid: str) -> None:
    """Stop and remove a container (best-effort; ignores errors)."""
    try:
        subprocess.run(["docker", "stop", cid], capture_output=True, timeout=30)
    except Exception:
        pass


def _docker_exec_login(
    cid: str,
    shell_cmd: str,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Run *shell_cmd* inside the container via a bash login shell.

    The login shell activates the conda ``testbed`` environment automatically,
    which is required for the project's Python and installed dependencies to be
    on the PATH.
    """
    return _run_cmd(
        ["docker", "exec", cid, "bash", "-lc", shell_cmd],
        timeout=timeout,
    )


def _docker_exec(
    cid: str,
    cmd: list[str],
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a command directly inside the container (no shell activation)."""
    return _run_cmd(["docker", "exec", cid, *cmd], timeout=timeout)


def _copy_patch_to_container(cid: str, patch_content: str) -> tuple[bool, str]:
    """Write *patch_content* to ``/tmp/patch.diff`` inside the container.

    Returns ``(success, error_message)``.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
        f.write(patch_content)
        local_path = f.name
    try:
        r = _run_cmd(
            ["docker", "cp", local_path, f"{cid}:/tmp/patch.diff"],
            timeout=30,
        )
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, ""
    finally:
        os.unlink(local_path)


def _copy_script_to_container(cid: str, script_content: str, remote_path: str) -> tuple[bool, str]:
    """Write *script_content* to *remote_path* inside the container.

    Returns ``(success, error_message)``.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script_content)
        local_path = f.name
    try:
        r = _run_cmd(
            ["docker", "cp", local_path, f"{cid}:{remote_path}"],
            timeout=30,
        )
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, ""
    finally:
        os.unlink(local_path)


def _apply_patch(cid: str) -> tuple[bool, str]:
    """Apply ``/tmp/patch.diff`` to ``/testbed`` inside the container.

    Tries ``git apply`` first; falls back to ``patch -p1``.
    Returns ``(success, error_message)``.
    """
    r = _docker_exec(cid, ["git", "-C", "/testbed", "apply", "/tmp/patch.diff"])
    if r.returncode == 0:
        return True, ""
    # Fallback: patch -p1
    r2 = _docker_exec_login(cid, "cd /testbed && patch -p1 < /tmp/patch.diff")
    if r2.returncode == 0:
        return True, ""
    err = (
        f"git apply: {r.stderr.strip()[:200]}\n"
        f"patch: {r2.stderr.strip()[:200]}"
    )
    return False, err


# ---------------------------------------------------------------------------
# Test output parser
# ---------------------------------------------------------------------------

def _parse_pytest_output(output: str) -> set[str]:
    """Extract failing test node IDs from pytest ``-v`` or ``-q`` output.

    Matches lines of the form::

        FAILED tests/foo/test_bar.py::TestClass::test_method - reason
        tests/foo/test_bar.py::TestClass::test_method FAILED

    Returns a set of node ID strings.
    """
    failures: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        # Format: "FAILED <node_id>"  (pytest -q output)
        m = re.match(r"^FAILED\s+(\S+)", line)
        if m:
            failures.add(m.group(1))
            continue
        # Format: "<node_id> FAILED"  (pytest -v output)
        m2 = re.match(r"^(\S+)\s+FAILED\s*$", line)
        if m2 and "::" in m2.group(1):
            failures.add(m2.group(1))
    return failures


# ---------------------------------------------------------------------------
# Phase 2.1 — Regression test check
# ---------------------------------------------------------------------------

def check_regression_tests(
    container_id: str,
    pass_to_pass: list[str],
    *,
    timeout: int = 120,
) -> CheckResult:
    """Run PASS_TO_PASS tests before/after the patch; reject on new failures.

    The patch (already copied to ``/tmp/patch.diff``) is applied once between
    the pre-patch and post-patch test runs.  Only tests that were passing
    before and fail after the patch count as regressions.

    Args:
        container_id: Running Docker container ID for the SWE-bench instance.
        pass_to_pass: List of pytest node IDs from the SWE-bench instance's
            ``PASS_TO_PASS`` field.  These are the tests expected to remain
            passing before and after a correct patch.
        timeout: Seconds allowed per test run (pre + post each get this budget).

    Returns:
        CheckResult with:
          SKIP   — if ``pass_to_pass`` is empty
          REJECT — if any previously-passing test now fails (regressions found)
          PASS   — if no regressions (pre-existing failures are ignored)
          ERROR  — if patch application or test execution fails unexpectedly
    """
    if not pass_to_pass:
        logger.debug("check_regression_tests: no PASS_TO_PASS tests — SKIP")
        return CheckResult(
            verdict="SKIP",
            check_name="regression_tests",
            message="no PASS_TO_PASS tests in instance",
        )

    test_ids_str = " ".join(pass_to_pass)
    logger.debug("check_regression_tests: %d test(s) to run", len(pass_to_pass))

    # ------------------------------------------------------------------
    # Pre-patch run
    # ------------------------------------------------------------------
    pre_cmd = (
        f"cd /testbed && python -m pytest {test_ids_str} "
        f"--tb=no -q 2>&1"
    )
    try:
        pre = _docker_exec_login(container_id, pre_cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("check_regression_tests: pre-patch run timed out")
        return CheckResult(
            verdict="ERROR",
            check_name="regression_tests",
            message="pre-patch test run timed out",
        )

    pre_failed = _parse_pytest_output(pre.stdout)
    logger.debug("check_regression_tests: pre-patch — %d failing", len(pre_failed))

    # ------------------------------------------------------------------
    # Apply patch
    # ------------------------------------------------------------------
    ok, err = _apply_patch(container_id)
    if not ok:
        logger.warning("check_regression_tests: patch apply failed — %s", err[:100])
        return CheckResult(
            verdict="ERROR",
            check_name="regression_tests",
            message=f"patch apply failed: {err}",
        )

    # ------------------------------------------------------------------
    # Post-patch run  (collect all failures, no -x)
    # ------------------------------------------------------------------
    post_cmd = (
        f"cd /testbed && python -m pytest {test_ids_str} "
        f"--tb=no -q 2>&1"
    )
    try:
        post = _docker_exec_login(container_id, post_cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("check_regression_tests: post-patch run timed out")
        return CheckResult(
            verdict="ERROR",
            check_name="regression_tests",
            message="post-patch test run timed out",
        )

    post_failed = _parse_pytest_output(post.stdout)
    logger.debug("check_regression_tests: post-patch — %d failing", len(post_failed))

    # ------------------------------------------------------------------
    # Delta: tests newly failing after the patch
    # ------------------------------------------------------------------
    new_failures = sorted(post_failed - pre_failed)

    if new_failures:
        sample = "; ".join(new_failures[:5])
        suffix = f" (+ {len(new_failures) - 5} more)" if len(new_failures) > 5 else ""
        msg = f"{len(new_failures)} regression(s): {sample}{suffix}"
        logger.info("check_regression_tests: REJECT — %s", msg)
        return CheckResult(verdict="REJECT", check_name="regression_tests", message=msg)

    msg = (
        f"{len(pre_failed)} pre-existing failure(s), 0 new regressions "
        f"across {len(pass_to_pass)} test(s)"
    )
    logger.debug("check_regression_tests: PASS — %s", msg)
    return CheckResult(verdict="PASS", check_name="regression_tests", message=msg)


# ---------------------------------------------------------------------------
# Phase 2.2 — Reproduction test check
# ---------------------------------------------------------------------------

def check_reproduction_test(
    container_id: str,
    repro_test_code: str,
    *,
    timeout: int = 60,
) -> CheckResult:
    """Run the reproduction test script on the already-patched container.

    The script must **pass** (exit 0) to confirm the patch fixes the bug.
    A non-zero exit code means the patch did not fix the issue, so the
    patch is rejected.

    **Pre-condition:** The patch must already be applied to the container
    (``check_regression_tests`` does this).

    Args:
        container_id: Running Docker container ID (patch already applied).
        repro_test_code: Full source of the reproduction test Python script.
        timeout: Seconds allowed for the script to run.

    Returns:
        CheckResult with:
          PASS   — script exits 0 (bug is fixed)
          REJECT — script exits non-zero (bug still present or wrong fix)
          ERROR  — infrastructure failure (copy, timeout, etc.)
    """
    remote_path = "/tmp/sieve_repro_test.py"
    ok, err = _copy_script_to_container(container_id, repro_test_code, remote_path)
    if not ok:
        logger.warning("check_reproduction_test: copy failed — %s", err)
        return CheckResult(
            verdict="ERROR",
            check_name="reproduction_test",
            message=f"failed to copy repro script: {err}",
        )

    run_cmd = f"cd /testbed && python {remote_path} 2>&1"
    try:
        r = _docker_exec_login(container_id, run_cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("check_reproduction_test: timed out after %ds", timeout)
        return CheckResult(
            verdict="ERROR",
            check_name="reproduction_test",
            message=f"repro script timed out after {timeout}s",
        )

    if r.returncode == 0:
        logger.debug("check_reproduction_test: PASS — bug is fixed")
        return CheckResult(
            verdict="PASS",
            check_name="reproduction_test",
            message="repro test passed (bug is fixed)",
        )

    # Script failed — capture the tail of output for the feedback message
    output_tail = r.stdout.strip()[-300:] if r.stdout.strip() else "(no output)"
    msg = f"repro test still fails (exit {r.returncode}): …{output_tail}"
    logger.info("check_reproduction_test: REJECT — %s", msg[:120])
    return CheckResult(verdict="REJECT", check_name="reproduction_test", message=msg)


# ---------------------------------------------------------------------------
# Phase 2.3 — Orchestrator
# ---------------------------------------------------------------------------

def run_dynamic_checks(
    instance: dict,
    patch_content: str,
    *,
    repro_test_code: str | None = None,
    timeout: int = 120,
) -> DynamicCheckResult:
    """Run all Layer-2 dynamic checks and return an aggregate result.

    Execution order
    ---------------
    1. Start a fresh Docker container for the SWE-bench instance (unpatched state).

    2. Copy the patch to ``/tmp/patch.diff`` inside the container.

    3. ``check_regression_tests`` — run PASS_TO_PASS tests, apply patch, re-run tests,
       report new failures.  This step owns the single patch application.
       Hard stop on REJECT: reproduction check is still run (patch is already applied).

    4. ``check_reproduction_test`` — run repro script on the now-patched container.
       Recorded as SKIP if no ``repro_test_code`` was provided.

    5. Aggregate verdicts: REJECT > FLAG > PASS; SKIP if all checks are SKIP;
       ERROR if container startup or patch copy failed.

    6. Stop container (always, in ``finally``).

    Args:
        instance: SWE-bench instance dict (must contain ``instance_id`` and
            ``PASS_TO_PASS``).  Obtained from ``sieve.utils.swebench.load_instances()``.
        patch_content: Raw unified-diff patch text (the ``model_patch`` field from
            ``preds.json``).
        repro_test_code: Source of the reproduction test Python script, or ``None``
            to skip the reproduction check.
        timeout: Seconds allowed per individual test run (pre-patch and post-patch
            regression runs each get this budget independently).

    Returns:
        ``DynamicCheckResult`` with the overall verdict and per-check details.
    """
    instance_id: str = instance["instance_id"]

    # ------------------------------------------------------------------
    # Parse PASS_TO_PASS from the instance (JSON-encoded list of node IDs)
    # ------------------------------------------------------------------
    raw_p2p = instance.get("PASS_TO_PASS", "[]")
    if isinstance(raw_p2p, list):
        pass_to_pass: list[str] = raw_p2p
    else:
        try:
            pass_to_pass = json.loads(raw_p2p)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "run_dynamic_checks: could not parse PASS_TO_PASS for %s — treating as empty",
                instance_id,
            )
            pass_to_pass = []

    # ------------------------------------------------------------------
    # Result-tracking helpers (mirror static.py pattern)
    # ------------------------------------------------------------------
    details: dict[str, CheckResult] = {}
    checks_passed: list[str] = []
    checks_failed: list[str] = []
    flags: list[str] = []

    def _record(cr: CheckResult) -> None:
        details[cr.check_name] = cr
        if cr.verdict == "PASS":
            checks_passed.append(cr.check_name)
        elif cr.verdict == "REJECT":
            checks_failed.append(cr.check_name)
        elif cr.verdict == "FLAG" and cr.message:
            flags.append(cr.message)
        # SKIP and ERROR are stored in details but don't update pass/fail lists

    def _make_result() -> DynamicCheckResult:
        if checks_failed:
            overall = "REJECT"
        elif flags:
            overall = "FLAG"
        elif checks_passed:
            overall = "PASS"
        else:
            # All checks were SKIP or ERROR
            any_error = any(cr.verdict == "ERROR" for cr in details.values())
            overall = "ERROR" if any_error else "SKIP"
        return DynamicCheckResult(
            verdict=overall,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            flags=flags,
            details=details,
        )

    # ------------------------------------------------------------------
    # Start container
    # ------------------------------------------------------------------
    img = _image_name(instance_id)
    logger.info("run_dynamic_checks: starting container for %s (%s)", instance_id, img)
    cid = _start_container(img)
    if cid is None:
        err_cr = CheckResult(
            verdict="ERROR",
            check_name="container_startup",
            message=f"failed to start Docker container for image {img}",
        )
        _record(err_cr)
        return _make_result()

    logger.debug("run_dynamic_checks: container %s started", cid[:12])

    try:
        # ------------------------------------------------------------------
        # Copy patch into container
        # ------------------------------------------------------------------
        ok, err = _copy_patch_to_container(cid, patch_content)
        if not ok:
            err_cr = CheckResult(
                verdict="ERROR",
                check_name="patch_copy",
                message=f"docker cp patch failed: {err}",
            )
            _record(err_cr)
            return _make_result()

        # ------------------------------------------------------------------
        # Phase 2.1 — Regression tests
        # (also applies the patch inside the container)
        # ------------------------------------------------------------------
        logger.info("run_dynamic_checks: running regression tests for %s", instance_id)
        cr_regression = check_regression_tests(
            container_id=cid,
            pass_to_pass=pass_to_pass,
            timeout=timeout,
        )
        _record(cr_regression)

        # ------------------------------------------------------------------
        # Phase 2.2 — Reproduction test (patch already applied above)
        # ------------------------------------------------------------------
        if repro_test_code is not None:
            logger.info("run_dynamic_checks: running reproduction test for %s", instance_id)
            cr_repro = check_reproduction_test(
                container_id=cid,
                repro_test_code=repro_test_code,
                timeout=60,
            )
        else:
            logger.debug("run_dynamic_checks: no repro test provided — SKIP")
            cr_repro = CheckResult(
                verdict="SKIP",
                check_name="reproduction_test",
                message="no reproduction test provided",
            )
        _record(cr_repro)

    finally:
        _stop_container(cid)
        logger.debug("run_dynamic_checks: container %s stopped", cid[:12])

    result = _make_result()
    logger.info(
        "run_dynamic_checks: %s → %s", instance_id, result.verdict
    )
    return result

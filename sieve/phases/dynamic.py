"""SIEVE Dynamic Checker — Layer 2a (regression) of the evaluation cascade.

Layer 2b (reproduction) lives in ``sieve/phases/reproduction.py``. This
module is responsible only for the PASS_TO_PASS regression check.

Verdict semantics (same as Layer 1):
    PASS | REJECT | FLAG | SKIP | ERROR
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field

from sieve.phases._docker import (
    apply_patch,
    copy_patch_to_container,
    docker_exec_login,
    image_name,
    parse_pytest_output,
    start_container,
    stop_container,
)

logger = logging.getLogger("sieve.phases.dynamic")


@dataclass
class CheckResult:
    verdict: str
    check_name: str
    message: str = ""


@dataclass
class DynamicCheckResult:
    verdict: str
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    details: dict[str, CheckResult] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def check_regression_tests(
    container_id: str,
    pass_to_pass: list[str],
    *,
    timeout: int = 120,
) -> CheckResult:
    """Run PASS_TO_PASS tests before/after the patch; reject on new failures."""
    if not pass_to_pass:
        return CheckResult(
            verdict="SKIP",
            check_name="regression_tests",
            message="no PASS_TO_PASS tests in instance",
        )

    test_ids_str = " ".join(pass_to_pass)

    pre_cmd = f"cd /testbed && python -m pytest {test_ids_str} --tb=no -q 2>&1"
    try:
        pre = docker_exec_login(container_id, pre_cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            verdict="ERROR",
            check_name="regression_tests",
            message="pre-patch test run timed out",
        )
    pre_failed = parse_pytest_output(pre.stdout)

    ok, err = apply_patch(container_id)
    if not ok:
        return CheckResult(
            verdict="ERROR",
            check_name="regression_tests",
            message=f"patch apply failed: {err}",
        )

    post_cmd = f"cd /testbed && python -m pytest {test_ids_str} --tb=no -q 2>&1"
    try:
        post = docker_exec_login(container_id, post_cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            verdict="ERROR",
            check_name="regression_tests",
            message="post-patch test run timed out",
        )
    post_failed = parse_pytest_output(post.stdout)

    new_failures = sorted(post_failed - pre_failed)
    if new_failures:
        sample = "; ".join(new_failures[:5])
        suffix = f" (+ {len(new_failures) - 5} more)" if len(new_failures) > 5 else ""
        msg = f"{len(new_failures)} regression(s): {sample}{suffix}"
        return CheckResult(verdict="REJECT", check_name="regression_tests", message=msg)

    msg = (
        f"{len(pre_failed)} pre-existing failure(s), 0 new regressions "
        f"across {len(pass_to_pass)} test(s)"
    )
    return CheckResult(verdict="PASS", check_name="regression_tests", message=msg)


def run_dynamic_checks(
    instance: dict,
    patch_content: str,
    *,
    timeout: int = 120,
) -> DynamicCheckResult:
    """Run the regression check and return an aggregate result."""
    instance_id: str = instance["instance_id"]

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

    def _make_result() -> DynamicCheckResult:
        if checks_failed:
            overall = "REJECT"
        elif flags:
            overall = "FLAG"
        elif checks_passed:
            overall = "PASS"
        else:
            any_error = any(cr.verdict == "ERROR" for cr in details.values())
            overall = "ERROR" if any_error else "SKIP"
        return DynamicCheckResult(
            verdict=overall,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            flags=flags,
            details=details,
        )

    img = image_name(instance_id)
    logger.info("run_dynamic_checks: starting container for %s (%s)", instance_id, img)
    cid = start_container(img)
    if cid is None:
        _record(CheckResult(
            verdict="ERROR",
            check_name="container_startup",
            message=f"failed to start Docker container for image {img}",
        ))
        return _make_result()

    try:
        ok, err = copy_patch_to_container(cid, patch_content)
        if not ok:
            _record(CheckResult(
                verdict="ERROR",
                check_name="patch_copy",
                message=f"docker cp patch failed: {err}",
            ))
            return _make_result()

        cr_regression = check_regression_tests(
            container_id=cid,
            pass_to_pass=pass_to_pass,
            timeout=timeout,
        )
        _record(cr_regression)
    finally:
        stop_container(cid)

    return _make_result()

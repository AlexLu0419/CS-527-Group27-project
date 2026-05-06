"""SIEVE Static Checker — Layer 1 of the evaluation cascade.

Implemented:
    Phase 1.1 — Patch Validity Check
        check_patch_applies  — git apply --check (dry-run)

    Phase 1.2 — Orchestration
        run_static_checks    — runs the patch-applies check and returns a
                               StaticCheckResult
        StaticCheckResult    — aggregate dataclass (verdict + per-check details)
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("sieve.phases.static")


# ---------------------------------------------------------------------------
# Per-check result type
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    """Result of a single static check."""

    verdict: str          # "PASS" | "REJECT" | "FLAG"
    check_name: str
    message: str = ""     # Human-readable detail (error text, file:line, etc.)


@dataclass
class StaticCheckResult:
    """Aggregate result returned by ``run_static_checks``.

    Fields
    ------
    verdict : str
        Overall verdict — ``"REJECT"`` if any check rejected the patch,
        ``"FLAG"`` if any check flagged warnings (but none rejected),
        ``"PASS"`` if all checks passed cleanly.
    checks_passed : list[str]
        Names of checks that returned PASS.
    checks_failed : list[str]
        Names of checks that returned REJECT (these triggered the overall REJECT).
    flags : list[str]
        Human-readable warning messages from checks that returned FLAG.
        Non-blocking: the pipeline can still accept the patch with flags.
    details : dict[str, CheckResult]
        Full ``CheckResult`` for every check that was executed, keyed by
        ``check_name``.
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
# 1.1 — Patch applicability
# ---------------------------------------------------------------------------

def check_patch_applies(repo_path: str | Path, patch_file: str | Path) -> CheckResult:
    """Check whether a patch applies cleanly to the repository.

    Runs ``git apply --check <patch_file>`` inside *repo_path*.  The check
    flag makes git do a dry-run without touching the working tree.

    Args:
        repo_path: Absolute path to the root of the cloned repository.
        patch_file: Path to the ``.patch`` / ``.diff`` file to test.

    Returns:
        CheckResult with verdict PASS or REJECT.
        On REJECT the ``message`` field contains the stderr output from git.
    """
    repo_path = Path(repo_path)
    patch_file = Path(patch_file)

    result = subprocess.run(
        ["git", "apply", "--check", str(patch_file)],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error_text = (result.stderr or result.stdout).strip()
        logger.debug("check_patch_applies: REJECT — %s", error_text)
        return CheckResult(
            verdict="REJECT",
            check_name="patch_applies",
            message=error_text,
        )

    logger.debug("check_patch_applies: PASS")
    return CheckResult(verdict="PASS", check_name="patch_applies")


# ---------------------------------------------------------------------------
# 1.2 — Orchestrator
# ---------------------------------------------------------------------------

def run_static_checks(
    repo_path: str | Path,
    patch_file: str | Path,
) -> StaticCheckResult:
    """Run the static layer (just the patch-applies dry-run) and return an
    aggregate result.

    Args:
        repo_path: Absolute path to the root of the cloned repository.
        patch_file: Path to the ``.patch`` / ``.diff`` file to evaluate.

    Returns:
        ``StaticCheckResult`` with the overall verdict and per-check details.
    """
    cpa = check_patch_applies(repo_path, patch_file)

    if cpa.verdict == "REJECT":
        return StaticCheckResult(
            verdict="REJECT",
            checks_failed=[cpa.check_name],
            details={cpa.check_name: cpa},
        )

    return StaticCheckResult(
        verdict="PASS",
        checks_passed=[cpa.check_name],
        details={cpa.check_name: cpa},
    )

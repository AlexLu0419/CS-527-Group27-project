"""SIEVE Static Checker — Layer 1 of the evaluation cascade.

Phase 1.1: Patch Validity Checks
    check_patch_applies  — git apply --check
    check_files_parse    — ast.parse / py_compile
"""

from __future__ import annotations

import ast
import logging
import subprocess
import sys
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# 1.1a — Patch applicability
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
# 1.1b — Syntax / parse check for modified files
# ---------------------------------------------------------------------------

def check_files_parse(
    repo_path: str | Path,
    changed_files: list[str | Path],
) -> CheckResult:
    """Verify that every modified Python file is syntactically valid.

    For each file we first attempt ``ast.parse`` (gives precise line numbers
    and a clean message) and fall back to ``python -m py_compile`` as a
    secondary signal (catches a slightly wider set of compile-time errors
    such as encoding issues that ast.parse doesn't always surface).

    Only ``.py`` files are checked; non-Python files are silently skipped.

    Args:
        repo_path: Absolute path to the repository root.  File paths that
            are relative will be resolved against this directory.
        changed_files: Iterable of file paths that were modified by the
            patch.  May be absolute or relative to *repo_path*.

    Returns:
        CheckResult with verdict PASS, or REJECT on the **first** file that
        fails to parse.  The ``message`` field contains
        ``<relative_path>:<lineno>: <error>``.
    """
    repo_path = Path(repo_path)

    for raw in changed_files:
        path = Path(raw)
        if not path.is_absolute():
            path = repo_path / path

        if path.suffix != ".py":
            continue

        if not path.exists():
            # File deleted by the patch — nothing to parse.
            continue

        # --- primary: ast.parse (best error messages) ---
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            rel = path.relative_to(repo_path) if path.is_relative_to(repo_path) else path
            msg = f"{rel}:{exc.lineno}: {exc.msg}"
            logger.debug("check_files_parse: REJECT (ast) — %s", msg)
            return CheckResult(
                verdict="REJECT",
                check_name="files_parse",
                message=msg,
            )

        # --- secondary: py_compile (catches encoding / codec errors) ---
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            rel = path.relative_to(repo_path) if path.is_relative_to(repo_path) else path
            error_text = (result.stderr or result.stdout).strip()
            # Extract line number from py_compile output if present
            # Typical format: "  File '<path>', line N"
            lineno = _extract_lineno(error_text) or "?"
            msg = f"{rel}:{lineno}: {error_text}"
            logger.debug("check_files_parse: REJECT (py_compile) — %s", msg)
            return CheckResult(
                verdict="REJECT",
                check_name="files_parse",
                message=msg,
            )

    logger.debug("check_files_parse: PASS (%d files checked)", len(list(changed_files)))
    return CheckResult(verdict="PASS", check_name="files_parse")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_lineno(py_compile_output: str) -> str | None:
    """Pull the line number out of a py_compile error string, if present.

    py_compile writes something like::

        File "<path>", line 42
          bad syntax here

    Returns the line number as a string, or None if not found.
    """
    for line in py_compile_output.splitlines():
        line = line.strip()
        if line.startswith("File ") and ", line " in line:
            parts = line.split(", line ")
            if len(parts) == 2:
                return parts[1].strip()
    return None

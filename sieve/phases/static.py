"""SIEVE Static Checker — Layer 1 of the evaluation cascade.

Implemented:
    Phase 1.1 — Patch Validity Checks
        check_patch_applies  — git apply --check (dry-run)
        check_files_parse    — ast.parse + py_compile on modified .py files

    Phase 1.2 — Lint Delta Check
        check_lint_delta     — flake8 before/after patch, new diagnostics only
                               Requires: pip install flake8 flake8-json

    Phase 1.3 — Semgrep Check
        check_semgrep        — semgrep p/python on modified .py files (post-patch)
                               Requires: pip install semgrep

Not yet implemented:
    Phase 1.4 — run_static_checks  (orchestrator + StaticCheckResult dataclass)
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("sieve.phases.static")

# Codes that indicate the patched code will fail at runtime.
REJECT_CODES = {
    "F404",  # future import after other statements (SyntaxError in Python)
    "F821",  # undefined name
    "F822",  # undefined name in __all__
    "F823",  # local variable referenced before assignment
    "F701",  # break outside loop
    "F702",  # continue outside loop
    "F704",  # yield outside function
    "F706",  # return outside function
    "F707",  # except: block not last handler
    "F502",  # % format expected mapping but got sequence
    "F503",  # % format expected sequence but got mapping
    "F505",  # % format missing named arguments
    "F507",  # % format placeholder/argument count mismatch
    "F508",  # % format with * specifier requires a sequence
    "F509",  # % format unsupported format character
    "F524",  # .format() missing argument
    "F621",  # too many expressions in star-unpacking
    "F622",  # two or more starred expressions in assignment
    "F831",  # duplicate argument name in function definition
    "F901",  # raise NotImplemented should be raise NotImplementedError
}

# Codes that are suspicious but might be intentional.
FLAG_CODES = {
    "F401",  # module imported but unused
    "F811",  # redefinition of unused name
    "F841",  # local variable assigned but never used
    "F842",  # local variable annotated but never used
    "F405",  # name may be undefined, or defined from star imports
    "F631",  # assert test is a tuple, always True
    "F634",  # if test is a tuple, always True
    "F632",  # compare literals with is / is not
    "F601",  # dict key repeated with different values
    "F602",  # dict key variable repeated with different values
    "F501",  # invalid % format literal
    "F504",  # % format unused named arguments
    "F521",  # .format() invalid format string
    "F522",  # .format() unused named arguments
    "F523",  # .format() unused positional arguments
    "F525",  # .format() mixing automatic and manual numbering
    "F541",  # f-string without placeholders
    "F542",  # t-string without placeholders
    "F824",  # global/nonlocal is unused
}

# Codes not useful for patch verification.
IGNORE_CODES = {
    "E501",  # line length (explicitly ignored as noisy)
    "F402",  # import shadowed by loop variable
    "F403",  # from module import * used
    "F406",  # from module import * only allowed at module level
    "F407",  # undefined __future__ feature
    "F721",  # syntax error in doctest
    "F722",  # syntax error in forward annotation
    "F723",  # syntax error in type comment
    "F633",  # use of >> invalid with print function
}


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
# 1.2 — Lint delta check (flake8 before vs after patch)
# ---------------------------------------------------------------------------

def check_lint_delta(
    repo_path: str | Path,
    changed_files: list[str | Path],
    patch_file: str | Path,
) -> CheckResult:
    """Report lint diagnostics introduced by the patch.

    Workflow:
      1) Run flake8 on changed files before applying patch.
      2) Apply patch with git apply.
      3) Run flake8 again on the same files.
      4) Report only NEW diagnostics (after - before).

    Verdict policy:
      - REJECT for new ``E9*`` diagnostics.
      - REJECT/FLAG/IGNORE for new ``F*`` diagnostics by code mapping.
      - PASS if no reportable new diagnostics are introduced.
    """
    repo_path = Path(repo_path)
    patch_file = Path(patch_file)

    before = _run_flake8_json(repo_path, changed_files)

    apply_result = subprocess.run(
        ["git", "apply", str(patch_file)],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if apply_result.returncode != 0:
        error_text = (apply_result.stderr or apply_result.stdout).strip()
        logger.debug("check_lint_delta: REJECT (git apply failed) — %s", error_text)
        return CheckResult(
            verdict="REJECT",
            check_name="lint_delta",
            message=f"patch apply failed during lint check: {error_text}",
        )

    after = _run_flake8_json(repo_path, changed_files)

    new_findings = sorted(after - before)
    reject_findings, flag_findings = _classify_lint_findings(new_findings)

    if reject_findings:
        logger.debug("check_lint_delta: REJECT (%d new reject diagnostics)", len(reject_findings))
        return CheckResult(
            verdict="REJECT",
            check_name="lint_delta",
            message=_format_lint_findings(reject_findings),
        )

    if flag_findings:
        logger.debug("check_lint_delta: FLAG (%d new flag diagnostics)", len(flag_findings))
        return CheckResult(
            verdict="FLAG",
            check_name="lint_delta",
            message=_format_lint_findings(flag_findings),
        )

    logger.debug("check_lint_delta: PASS (no new reportable diagnostics)")
    return CheckResult(verdict="PASS", check_name="lint_delta")


# ---------------------------------------------------------------------------
# 1.3 — Semgrep check
# ---------------------------------------------------------------------------

def check_semgrep(
    repo_path: str | Path,
    changed_files: list[str | Path],
    config: str = "p/python",
) -> CheckResult:
    """Run Semgrep on modified Python files and flag any rule matches.

    Uses the given Semgrep registry config (default: ``p/python``) on the
    changed files as they currently exist on disk (post-patch).  All findings
    are reported as FLAG — Semgrep results alone never REJECT a patch.

    Args:
        repo_path: Absolute path to the repository root.
        changed_files: Files modified by the patch (absolute or relative to
            *repo_path*).  Non-``.py`` files and deleted files are skipped.
        config: Semgrep ``--config`` value.  Can be a registry shorthand
            (``"p/python"``, ``"p/django"``) or a path to a local YAML file.

    Returns:
        CheckResult with verdict FLAG (findings present) or PASS (no findings).
        On semgrep execution failure the verdict is PASS with a warning logged,
        so a missing/broken semgrep binary never blocks the pipeline.
    """
    repo_path = Path(repo_path)

    file_args: list[str] = []
    for raw in changed_files:
        p = Path(raw)
        if not p.is_absolute():
            p = repo_path / p
        if not p.exists() or p.suffix != ".py":
            continue
        rel = p.relative_to(repo_path) if p.is_relative_to(repo_path) else p
        file_args.append(str(rel))

    if not file_args:
        logger.debug("check_semgrep: PASS (no .py files to check)")
        return CheckResult(verdict="PASS", check_name="semgrep", message="(no .py files)")

    result = subprocess.run(
        ["semgrep", "--config", config, "--json", "--quiet", *file_args],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )

    # semgrep exits 0 (no findings), 1 (findings present), or 2+ (error).
    # Treat exit code 2+ as a non-fatal execution problem.
    output = result.stdout.strip()
    if result.returncode >= 2 or not output:
        if result.returncode >= 2:
            logger.warning(
                "check_semgrep: semgrep exited with code %d — %s",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
        else:
            logger.debug("check_semgrep: PASS (empty semgrep output)")
        return CheckResult(verdict="PASS", check_name="semgrep")

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        logger.warning("check_semgrep: failed to parse semgrep JSON output")
        return CheckResult(verdict="PASS", check_name="semgrep")

    # Surface any semgrep-level errors as warnings but keep going.
    for err in parsed.get("errors", []):
        logger.warning("check_semgrep: semgrep error: %s", err.get("message", err))

    results = parsed.get("results", [])
    if not results:
        logger.debug("check_semgrep: PASS (no findings)")
        return CheckResult(verdict="PASS", check_name="semgrep")

    lines: list[str] = []
    for r in results:
        path     = r.get("path", "?")
        line     = r.get("start", {}).get("line", "?")
        rule_id  = r.get("check_id", "?")
        message  = r.get("extra", {}).get("message", "").strip()
        lines.append(f"{path}:{line}: [{rule_id}] {message}")

    logger.debug("check_semgrep: FLAG (%d findings)", len(lines))
    return CheckResult(
        verdict="FLAG",
        check_name="semgrep",
        message="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_flake8_json(
    repo_path: Path,
    changed_files: list[str | Path],
) -> set[tuple[str, str, int, int, str]]:
    """Run flake8 and normalize diagnostics into a hashable set.

    Returned tuple layout:
      (code, relative_path, line_number, column_number, text)
    """
    file_args: list[str] = []
    for raw in changed_files:
        p = Path(raw)
        if not p.is_absolute():
            p = repo_path / p
        if not p.exists() or p.suffix != ".py":
            continue
        rel = p.relative_to(repo_path) if p.is_relative_to(repo_path) else p
        file_args.append(str(rel))

    if not file_args:
        return set()

    result = subprocess.run(
        ["flake8", "--select=F,E9", "--format=json", *file_args],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )

    # flake8 exits non-zero when issues exist; that's expected for this check.
    output = result.stdout.strip()
    if not output:
        return set()

    findings: set[tuple[str, str, int, int, str]] = set()
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        logger.warning("flake8 json output parse failed; treating as no findings")
        return set()

    for filename, diagnostics in parsed.items():
        for diag in diagnostics:
            code = str(diag.get("code", "")).strip()
            findings.add(
                (
                    code,
                    str(filename),
                    int(diag.get("line_number", 0)),
                    int(diag.get("column_number", 0)),
                    str(diag.get("text", "")).strip(),
                )
            )
    return findings


def _format_lint_findings(findings: list[tuple[str, str, int, int, str]]) -> str:
    """Format diagnostics for CheckResult.message."""
    lines = [
        f"{path}:{line}:{col}: {code} {text}"
        for code, path, line, col, text in findings
    ]
    return "\n".join(lines)


def _classify_lint_findings(
    findings: list[tuple[str, str, int, int, str]],
) -> tuple[list[tuple[str, str, int, int, str]], list[tuple[str, str, int, int, str]]]:
    """Split findings into reject-level and flag-level diagnostics."""
    reject_findings: list[tuple[str, str, int, int, str]] = []
    flag_findings: list[tuple[str, str, int, int, str]] = []

    for finding in findings:
        code = finding[0]
        if code in IGNORE_CODES:
            continue
        if code.startswith("E9"):
            reject_findings.append(finding)
            continue
        if code in REJECT_CODES:
            reject_findings.append(finding)
            continue
        if code in FLAG_CODES:
            flag_findings.append(finding)
            continue
        if code.startswith("F"):
            # Conservative default: unknown F-codes are surfaced as FLAG.
            flag_findings.append(finding)
            continue

    return reject_findings, flag_findings


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

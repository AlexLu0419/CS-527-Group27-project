"""SIEVE Static Checker — Layer 1 of the evaluation cascade.

Implemented:
    Phase 1.1 — Patch Validity Checks
        check_patch_applies  — git apply --check (dry-run)
        check_files_parse    — ast.parse + py_compile on modified .py files

    Phase 1.2 — Lint Delta Check
        check_lint_delta     — flake8 before/after patch, new diagnostics only
                               Requires: pip install flake8 flake8-json

    Phase 1.3 — Semgrep Check
        check_semgrep        — semgrep p/python before/after delta (or post-only)
                               Requires: pip install semgrep

    Phase 1.4 — Orchestration
        run_static_checks    — runs all checks in order, returns StaticCheckResult
        StaticCheckResult    — aggregate dataclass (verdict + per-check details)
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass, field
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
        ``check_name``.  Checks skipped due to an earlier REJECT are absent.
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
# 1.3 — Semgrep check (with optional delta mode)
# ---------------------------------------------------------------------------

def check_semgrep(
    repo_path: str | Path,
    changed_files: list[str | Path],
    config: str = "p/python",
    patch_file: str | Path | None = None,
) -> CheckResult:
    """Run Semgrep on modified Python files and flag any *new* rule matches.

    When *patch_file* is provided the check runs in **delta mode**: semgrep is
    executed on the changed files both before and after applying the patch, and
    only findings that are absent from the pre-patch baseline are reported.
    This prevents pre-existing findings in the same file from being surfaced as
    false positives — the same motivation that drives the before/after design of
    ``check_lint_delta``.

    When *patch_file* is ``None`` the function runs in **post-patch-only mode**:
    semgrep is executed on the files as they currently exist on disk (legacy
    behaviour).  This may produce false positives if the file already contained
    Semgrep findings before the patch was applied.

    Finding deduplication in delta mode uses the fingerprint
    ``(rule_id, path, start_line)``.  The message text is intentionally omitted
    from the fingerprint because it can vary slightly when surrounding context
    changes after a patch shifts line numbers.

    All findings are reported as FLAG — Semgrep results alone never REJECT a
    patch.  On semgrep execution failure the verdict is PASS with a warning
    logged, so a missing/broken semgrep binary never blocks the pipeline.

    Args:
        repo_path: Absolute path to the repository root.
        changed_files: Files modified by the patch (absolute or relative to
            *repo_path*).  Non-``.py`` files and deleted files are skipped.
        config: Semgrep ``--config`` value.  Can be a registry shorthand
            (``"p/python"``, ``"p/django"``) or a path to a local YAML file.
        patch_file: Path to the patch file.  When supplied the function applies
            the patch internally (via ``git apply``) and computes only the delta
            of new findings, exactly like ``check_lint_delta``.  Callers must
            ensure the patch has **not** been applied yet when passing this
            argument.  Omit (or pass ``None``) when the patch is already on disk.

    Returns:
        CheckResult with verdict FLAG (new findings present) or PASS (no new
        findings).
    """
    repo_path = Path(repo_path)

    # Resolve which .py files we actually care about (same for both modes).
    file_args: list[str] = []
    for raw in changed_files:
        p = Path(raw)
        if not p.is_absolute():
            p = repo_path / p
        if p.suffix != ".py":
            continue
        # In pre-patch mode the file may not exist yet (new file); we still
        # pass it so the post-patch run can find it.
        rel = p.relative_to(repo_path) if p.is_relative_to(repo_path) else p
        file_args.append(str(rel))

    if not file_args:
        logger.debug("check_semgrep: PASS (no .py files to check)")
        return CheckResult(verdict="PASS", check_name="semgrep", message="(no .py files)")

    # ------------------------------------------------------------------
    # Delta mode: run before → apply patch → run after → subtract
    # ------------------------------------------------------------------
    if patch_file is not None:
        patch_file = Path(patch_file)

        # 1. Pre-patch baseline (files that do not exist yet return empty set).
        before_results = _run_semgrep_json(repo_path, file_args, config)

        # 2. Apply the patch.
        apply_result = subprocess.run(
            ["git", "apply", str(patch_file)],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if apply_result.returncode != 0:
            error_text = (apply_result.stderr or apply_result.stdout).strip()
            logger.debug("check_semgrep: REJECT (git apply failed) — %s", error_text)
            return CheckResult(
                verdict="REJECT",
                check_name="semgrep",
                message=f"patch apply failed during semgrep check: {error_text}",
            )

        # 3. Post-patch findings.
        after_results = _run_semgrep_json(repo_path, file_args, config)

        # 4. Keep only findings whose fingerprint was not in the baseline.
        before_fps = {_semgrep_fingerprint(r) for r in before_results}
        new_results = [r for r in after_results if _semgrep_fingerprint(r) not in before_fps]

        if not new_results:
            logger.debug("check_semgrep: PASS (no new findings after delta)")
            return CheckResult(verdict="PASS", check_name="semgrep")

        lines: list[str] = []
        for r in new_results:
            path    = r.get("path", "?")
            line    = r.get("start", {}).get("line", "?")
            rule_id = r.get("check_id", "?")
            message = r.get("extra", {}).get("message", "").strip()
            lines.append(f"{path}:{line}: [{rule_id}] {message}")

        logger.debug("check_semgrep: FLAG (%d new findings, delta mode)", len(lines))
        return CheckResult(
            verdict="FLAG",
            check_name="semgrep",
            message="\n".join(lines),
        )

    # ------------------------------------------------------------------
    # Post-patch-only mode (legacy — no patch_file supplied)
    # ------------------------------------------------------------------
    # Filter to files that actually exist on disk now.
    existing_args = [a for a in file_args if (repo_path / a).exists()]
    if not existing_args:
        logger.debug("check_semgrep: PASS (no .py files exist on disk)")
        return CheckResult(verdict="PASS", check_name="semgrep", message="(no .py files)")

    post_results = _run_semgrep_json(repo_path, existing_args, config)

    if not post_results:
        logger.debug("check_semgrep: PASS (no findings, post-only mode)")
        return CheckResult(verdict="PASS", check_name="semgrep")

    lines = []
    for r in post_results:
        path    = r.get("path", "?")
        line    = r.get("start", {}).get("line", "?")
        rule_id = r.get("check_id", "?")
        message = r.get("extra", {}).get("message", "").strip()
        lines.append(f"{path}:{line}: [{rule_id}] {message}")

    logger.debug("check_semgrep: FLAG (%d findings, post-only mode)", len(lines))
    return CheckResult(
        verdict="FLAG",
        check_name="semgrep",
        message="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# 1.4 — Orchestrator
# ---------------------------------------------------------------------------

def run_static_checks(
    repo_path: str | Path,
    patch_file: str | Path,
    config: str = "p/python",
) -> StaticCheckResult:
    """Run all Layer-1 static checks in order and return an aggregate result.

    Execution order
    ---------------
    1. ``check_patch_applies`` — dry-run ``git apply --check`` (no disk change).
       Hard stop on REJECT: remaining checks are skipped.

    2. Pre-patch baselines collected via private helpers (flake8 + semgrep).
       This must happen before the patch is applied so delta subtraction works.

    3. ``git apply`` — the patch is applied **once** here.  Individual
       check functions (``check_lint_delta``, ``check_semgrep(patch_file=…)``)
       each apply the patch internally; calling them from the orchestrator would
       double-apply.  Instead the orchestrator applies once and uses the private
       helpers directly for delta computation.

    4. ``check_files_parse`` — syntax check on post-patch files.
       Hard stop on REJECT: lint/semgrep delta steps are skipped.

    5. Lint delta computed from pre/post baselines → REJECT or FLAG.

    6. Semgrep delta computed from pre/post baselines → FLAG only.

    7. Results aggregated into a single ``StaticCheckResult``.

    Args:
        repo_path: Absolute path to the root of the cloned repository.
        patch_file: Path to the ``.patch`` / ``.diff`` file to evaluate.
        config: Semgrep ``--config`` value (default ``"p/python"``).

    Returns:
        ``StaticCheckResult`` with the overall verdict and per-check details.
    """
    repo_path = Path(repo_path)
    patch_file = Path(patch_file)

    details: dict[str, CheckResult] = {}
    checks_passed: list[str] = []
    checks_failed: list[str] = []
    flags: list[str] = []

    # Helper: record a CheckResult and update the tracking lists.
    def _record(cr: CheckResult) -> None:
        details[cr.check_name] = cr
        if cr.verdict == "PASS":
            checks_passed.append(cr.check_name)
        elif cr.verdict == "REJECT":
            checks_failed.append(cr.check_name)
        elif cr.verdict == "FLAG" and cr.message:
            flags.append(cr.message)

    def _make_result() -> StaticCheckResult:
        if checks_failed:
            overall = "REJECT"
        elif flags:
            overall = "FLAG"
        else:
            overall = "PASS"
        return StaticCheckResult(
            verdict=overall,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            flags=flags,
            details=details,
        )

    # ------------------------------------------------------------------
    # Step 1 — Patch applicability (dry-run)
    # ------------------------------------------------------------------
    cpa = check_patch_applies(repo_path, patch_file)
    _record(cpa)
    if cpa.verdict == "REJECT":
        logger.debug("run_static_checks: stopping after patch_applies REJECT")
        return _make_result()

    # ------------------------------------------------------------------
    # Step 2 — Collect pre-patch baselines
    # ------------------------------------------------------------------
    changed_files = _extract_changed_files(patch_file)
    py_files = [f for f in changed_files if f.endswith(".py")]

    pre_lint: set[tuple[str, str, int, int, str]] = set()
    pre_semgrep: list[dict] = []
    if py_files:
        pre_lint = _run_flake8_json(repo_path, py_files)
        pre_semgrep = _run_semgrep_json(repo_path, py_files, config)

    # ------------------------------------------------------------------
    # Step 3 — Apply the patch (single apply owned by orchestrator)
    # ------------------------------------------------------------------
    apply_result = subprocess.run(
        ["git", "apply", str(patch_file)],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if apply_result.returncode != 0:
        error_text = (apply_result.stderr or apply_result.stdout).strip()
        logger.debug("run_static_checks: git apply failed — %s", error_text)
        cr = CheckResult(
            verdict="REJECT",
            check_name="patch_applies",
            message=f"patch apply failed (orchestrator): {error_text}",
        )
        _record(cr)
        return _make_result()

    # ------------------------------------------------------------------
    # Step 4 — Syntax / parse check (post-patch files on disk)
    # ------------------------------------------------------------------
    cfp = check_files_parse(repo_path, changed_files)
    _record(cfp)
    if cfp.verdict == "REJECT":
        logger.debug("run_static_checks: stopping after files_parse REJECT")
        return _make_result()

    # ------------------------------------------------------------------
    # Step 5 — Lint delta (pre-patch baseline already collected)
    # ------------------------------------------------------------------
    if not py_files:
        lint_cr = CheckResult(verdict="PASS", check_name="lint_delta",
                              message="(no .py files)")
    else:
        post_lint = _run_flake8_json(repo_path, py_files)
        new_lint = sorted(post_lint - pre_lint)
        reject_lint, flag_lint = _classify_lint_findings(new_lint)

        if reject_lint:
            lint_cr = CheckResult(
                verdict="REJECT",
                check_name="lint_delta",
                message=_format_lint_findings(reject_lint),
            )
        elif flag_lint:
            lint_cr = CheckResult(
                verdict="FLAG",
                check_name="lint_delta",
                message=_format_lint_findings(flag_lint),
            )
        else:
            lint_cr = CheckResult(verdict="PASS", check_name="lint_delta")

    _record(lint_cr)

    # ------------------------------------------------------------------
    # Step 6 — Semgrep delta (pre-patch baseline already collected)
    # ------------------------------------------------------------------
    if not py_files:
        semgrep_cr = CheckResult(verdict="PASS", check_name="semgrep",
                                 message="(no .py files)")
    else:
        post_semgrep = _run_semgrep_json(repo_path, py_files, config)
        pre_fps = {_semgrep_fingerprint(r) for r in pre_semgrep}
        new_semgrep = [r for r in post_semgrep if _semgrep_fingerprint(r) not in pre_fps]

        if new_semgrep:
            sg_lines = [
                f"{r.get('path', '?')}:{r.get('start', {}).get('line', '?')}: "
                f"[{r.get('check_id', '?')}] "
                f"{r.get('extra', {}).get('message', '').strip()}"
                for r in new_semgrep
            ]
            semgrep_cr = CheckResult(
                verdict="FLAG",
                check_name="semgrep",
                message="\n".join(sg_lines),
            )
        else:
            semgrep_cr = CheckResult(verdict="PASS", check_name="semgrep")

    _record(semgrep_cr)

    # ------------------------------------------------------------------
    # Step 7 — Aggregate
    # ------------------------------------------------------------------
    return _make_result()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_changed_files(patch_file: Path) -> list[str]:
    """Parse a unified diff and return all modified file paths.

    Returns paths relative to the repository root (``b/`` prefix stripped,
    trailing timestamps removed).  ``/dev/null`` entries (new-file or
    deleted-file markers) are excluded.  Order matches the patch file.
    """
    files: list[str] = []
    seen: set[str] = set()
    for line in patch_file.read_text(errors="replace").splitlines():
        if not line.startswith("+++ "):
            continue
        # Strip the leading '+++ ' and any trailing tab+timestamp
        path = line[4:].split("\t")[0].strip()
        # Strip git diff 'b/' prefix
        if path.startswith("b/"):
            path = path[2:]
        if not path or path.startswith("/dev/"):
            continue
        if path not in seen:
            seen.add(path)
            files.append(path)
    return files


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


def _run_semgrep_json(
    repo_path: Path,
    file_args: list[str],
    config: str = "p/python",
) -> list[dict]:
    """Run semgrep and return its raw ``results`` list.

    *file_args* must be paths relative to *repo_path* (already filtered to
    ``.py`` files).  Files that do not yet exist on disk are silently ignored by
    semgrep itself.

    Returns an empty list on execution failure or empty output — callers should
    treat that as "no findings" rather than an error.
    """
    if not file_args:
        return []

    result = subprocess.run(
        ["semgrep", "--config", config, "--json", "--quiet", *file_args],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )

    # semgrep exits 0 (no findings), 1 (findings), 2+ (error).
    output = result.stdout.strip()
    if result.returncode >= 2 or not output:
        if result.returncode >= 2:
            logger.warning(
                "_run_semgrep_json: semgrep exited with code %d — %s",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
        return []

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        logger.warning("_run_semgrep_json: failed to parse semgrep JSON output")
        return []

    for err in parsed.get("errors", []):
        logger.warning("_run_semgrep_json: semgrep error: %s", err.get("message", err))

    return parsed.get("results", [])


def _semgrep_fingerprint(result: dict) -> tuple[str, str, int]:
    """Stable fingerprint for a single semgrep result dict.

    Uses ``(rule_id, path, start_line)`` — intentionally omits the message text
    because it can change when surrounding lines shift after a patch is applied.
    """
    return (
        result.get("check_id", ""),
        result.get("path", ""),
        result.get("start", {}).get("line", 0),
    )


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

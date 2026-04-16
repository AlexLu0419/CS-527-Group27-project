"""Unit tests for sieve/phases/static.py — Phase 1.4 orchestration.

Each test creates a minimal git repository in a pytest tmp_path directory,
writes a seed Python file, commits it, then creates a patch and calls
run_static_checks() to verify the overall verdict and per-check details.

Tests
-----
test_syntax_error_patch      — broken Python syntax → REJECT at files_parse
test_clean_patch             — correct patch with no issues → PASS
test_bare_except_flag        — bare except: clause → FLAG from semgrep
                               (skipped if semgrep binary is not available)
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from sieve.phases.static import StaticCheckResult, run_static_checks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command inside *repo*, raising on non-zero exit."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed:\n{result.stderr}"
    )
    return result


def _init_repo(repo: Path) -> None:
    """Initialise a minimal git repo with a committer identity."""
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@sieve.test")
    _git(repo, "config", "user.name", "SIEVE Test")


def _commit(repo: Path, message: str = "initial") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _make_patch(repo: Path, patch_path: Path) -> None:
    """Generate a unified diff of the last commit vs its parent (or index)."""
    result = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    patch_path.write_text(result.stdout)


def _make_patch_from_index(repo: Path, patch_path: Path) -> None:
    """Generate a diff of staged changes against HEAD (for the first patch after init)."""
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    patch_path.write_text(result.stdout)


def _setup_repo_with_seed(tmp_path: Path, seed_content: str) -> tuple[Path, Path]:
    """Create a repo, commit a seed foo.py, return (repo_path, repo/foo.py)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    foo = repo / "foo.py"
    foo.write_text(textwrap.dedent(seed_content))
    _commit(repo, "seed")
    return repo, foo


def _semgrep_available() -> bool:
    return shutil.which("semgrep") is not None


# ---------------------------------------------------------------------------
# Test 1 — Broken syntax → REJECT at files_parse
# ---------------------------------------------------------------------------

def test_syntax_error_patch(tmp_path: Path) -> None:
    """A patch that introduces a syntax error must be REJECTed at files_parse."""
    seed = """\
        def greet(name):
            return "Hello, " + name
    """
    repo, foo = _setup_repo_with_seed(tmp_path, seed)

    # Overwrite foo.py with broken syntax, commit, extract patch
    foo.write_text(textwrap.dedent("""\
        def greet(name):
            return "Hello, " + name

        def (   # intentionally broken
    """))
    _commit(repo, "introduce syntax error")

    patch_file = tmp_path / "bad_syntax.patch"
    _make_patch(repo, patch_file)

    # Reset repo to pre-patch state so run_static_checks can apply the patch
    _git(repo, "reset", "--hard", "HEAD~1")

    result = run_static_checks(repo, patch_file)

    assert isinstance(result, StaticCheckResult)
    assert result.verdict == "REJECT", f"Expected REJECT, got {result.verdict}"
    assert "files_parse" in result.checks_failed, (
        f"Expected files_parse in checks_failed, got {result.checks_failed}"
    )
    assert "files_parse" in result.details
    assert result.details["files_parse"].verdict == "REJECT"


# ---------------------------------------------------------------------------
# Test 2 — Clean patch → PASS
# ---------------------------------------------------------------------------

def test_clean_patch(tmp_path: Path) -> None:
    """A well-formed patch that introduces no issues must PASS all checks."""
    seed = """\
        def add(x, y):
            result = x + y
            return result
    """
    repo, foo = _setup_repo_with_seed(tmp_path, seed)

    # Rename the local variable — semantically clean change
    foo.write_text(textwrap.dedent("""\
        def add(x, y):
            total = x + y
            return total
    """))
    _commit(repo, "rename variable")

    patch_file = tmp_path / "clean.patch"
    _make_patch(repo, patch_file)

    _git(repo, "reset", "--hard", "HEAD~1")

    result = run_static_checks(repo, patch_file)

    assert isinstance(result, StaticCheckResult)
    assert result.verdict == "PASS", (
        f"Expected PASS, got {result.verdict}. "
        f"Failed: {result.checks_failed}. Flags: {result.flags}"
    )
    assert not result.checks_failed
    # Both lint_delta and semgrep should be in the passed list
    assert "patch_applies" in result.checks_passed
    assert "files_parse" in result.checks_passed
    assert "lint_delta" in result.checks_passed


# ---------------------------------------------------------------------------
# Test 3 — bare except: → FLAG from semgrep
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _semgrep_available(), reason="semgrep binary not found")
def test_subprocess_shell_true_flag(tmp_path: Path) -> None:
    """A patch introducing subprocess shell=True must be FLAGged by semgrep.

    The rule ``python.lang.security.audit.subprocess-shell-true`` in the
    ``p/python`` registry reliably fires on ``subprocess.run(..., shell=True)``.
    The overall verdict must be FLAG (not REJECT) — semgrep never rejects.
    """
    seed = """\
        import subprocess

        def run_cmd(cmd):
            return subprocess.run(cmd, check=True)
    """
    repo, foo = _setup_repo_with_seed(tmp_path, seed)

    # Add shell=True — triggers python.lang.security.audit.subprocess-shell-true
    foo.write_text(textwrap.dedent("""\
        import subprocess

        def run_cmd(cmd):
            return subprocess.run(cmd, shell=True, check=True)
    """))
    _commit(repo, "add shell=True")

    patch_file = tmp_path / "shell_true.patch"
    _make_patch(repo, patch_file)

    _git(repo, "reset", "--hard", "HEAD~1")

    result = run_static_checks(repo, patch_file)

    assert isinstance(result, StaticCheckResult)
    assert result.verdict == "FLAG", (
        f"Expected FLAG, got {result.verdict}. Details: {result.details}"
    )
    assert not result.checks_failed, (
        f"No check should REJECT for shell=True, got {result.checks_failed}"
    )
    assert result.flags, "Expected at least one FLAG message from semgrep"
    assert "semgrep" in result.details
    assert result.details["semgrep"].verdict == "FLAG"
    assert "subprocess-shell-true" in result.details["semgrep"].message

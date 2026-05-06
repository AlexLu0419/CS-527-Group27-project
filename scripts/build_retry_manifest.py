#!/usr/bin/env python3
"""build_retry_manifest.py

Reads the SIEVE cascade result JSONs, identifies every instance whose final
cascade verdict is anything other than PASS, and writes a per-instance
feedback payload assembled from (a) the agent's prior patch and (b) the
three cascade layers {static, dynamic_regression, reproduction}, in
deterministic block order.

Retry inclusion rule: an instance is retried iff its final cascade verdict
is FAIL, ERROR, MISSING, or UNCERTAIN. Only PASS is left as-is. This is a
single roster — empty patches surface as ERROR at the static layer; harness
errors surface as MISSING (no validator row).

Design
------
- Blocks are assembled in a fixed order regardless of which layer rejected:
    1. "# Your previous patch (first-run attempt):"  — from preds.json
    2. "# Static check:"                             — from static results
    3. "# Regression check:"                         — from dynamic_regression
    4. "# Reproduction check:"                       — from reproduction results
- Judge output is **excluded** from retry feedback on purpose: the retry
  solver should see raw cascade signals, not an LLM narrative that may over-
  commit. Judge still gates the cascade verdict (FAIL vs UNCERTAIN routing).
- FAIL_TO_PASS is **excluded** — that test set is a benchmark artifact, not a
  signal available to a real-world SWE agent.
- On UNCERTAIN_ZERO_SIGNAL, the reproduction block emits an honest fallback
  ("no verified reproduction test available; treat as open re-investigation")
  rather than synthesized narrative, addressing the v2 over-commitment
  regression mode.
- On FAIL with a verified gated test, the reproduction block includes a
  compact pointer to the highest-weight failing reproduction test and the
  observed stdout_tail on the patched code.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_STATIC = REPO_ROOT / "runs/static_checks_validation_gpt5mini/results.json"
DEFAULT_DYNREG = REPO_ROOT / "runs/dynamic_regression_validation_gpt5mini/results.json"
DEFAULT_REPRO = REPO_ROOT / "runs/reproduction_validation_gpt5mini/results.json"
DEFAULT_JUDGE = REPO_ROOT / "runs/judge_validation_gpt5mini/results.json"
DEFAULT_PREDS = REPO_ROOT / "runs/swe-verified_50_gpt5-mini/preds.json"
DEFAULT_PHASE_A_CACHE = REPO_ROOT / "runs/phase_a_cache"
DEFAULT_OUT = REPO_ROOT / "runs/retry_gpt5mini"
DEFAULT_FOCAL_SNIPPETS = REPO_ROOT / "runs/focal_snippets_cache"

# Aggregate cap on the assembled feedback payload.  Individual blocks have
# their own sub-caps (below).
MAX_FEEDBACK_CHARS = 9000

# Per-block sub-caps.  Chosen so the sum comfortably fits MAX_FEEDBACK_CHARS
# with headroom for block headers and separators.
PRIOR_PATCH_MAX_CHARS = 3000    
STATIC_MSG_MAX_CHARS = 1000
REGRESSION_MSG_MAX_CHARS = 2000
REPRO_STDOUT_MAX_CHARS = 1500
REPRO_TEST_SOURCE_MAX_CHARS = 3600
REPRO_TEST_SOURCE_MAX_LINES = 60
JUDGE_BLOCK_MAX_CHARS = 1300
OVERSIZED_PATCH_LINE_THRESHOLD = 100
FOCAL_SNIPPET_MAX_CHARS = 2200
FOCAL_SNIPPET_MAX_ENTRIES = 3
FOCAL_SYMBOL_MAP_MAX_SYMBOLS = 12
FOCAL_SYMBOL_MAP_MAX_CHARS = 800
MULTI_FILE_HINT_MIN_PATHS = 2


# ----------------------------------------------------------------------------
# IO helpers
# ----------------------------------------------------------------------------

def _index(path: Path) -> dict:
    if not path.exists():
        return {}
    return {r["instance_id"]: r for r in json.loads(path.read_text())}


def _load_preds(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _load_phase_a(cache_dir: Path, iid: str) -> dict | None:
    p = cache_dir / f"{iid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[: max(0, limit - 80)]
    return head + "\n... [truncated]"


# ----------------------------------------------------------------------------
# Patch hygiene (minimal — avoid hard import from sieve to keep this script
# runnable without the sieve deps installed).  Mirrors sieve.judge.strip.
# ----------------------------------------------------------------------------

_PATCH_PROVENANCE_RE = re.compile(
    r"^(From|Date|Subject|Signed-off-by|Co-authored-by|Author|Reviewed-by)\s*:.*$",
    re.MULTILINE | re.IGNORECASE,
)
_FIX_COMMENT_RE = re.compile(
    r"^\+?\s*(?:#|//)\s*(?:Fix|Fixes|Fixed|Resolves?|Closes?)\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_ISSUE_REF_RE = re.compile(r"\bissue\s*#\s*\d+\b", re.IGNORECASE)
_DIFF_PLUS_RE = re.compile(r"^\+\+\+ b/(?P<path>[^\s]+)\s*$", re.MULTILINE)
_PY_PATH_RE = re.compile(
    r"(?:(?<=[\s(\"'`])|^)([a-zA-Z_][\w/]*\.py)(?=[\s)\"'`:,.]|$)"
)


def _patch_files(patch: str) -> list[str]:
    return sorted({
        m.group("path") for m in _DIFF_PLUS_RE.finditer(patch or "")
        if m.group("path") and m.group("path") != "dev/null"
    })


def _strip_patch_metadata(diff: str) -> str:
    if not diff:
        return ""
    out = _PATCH_PROVENANCE_RE.sub("", diff)
    out = _FIX_COMMENT_RE.sub("", out)
    out = _ISSUE_REF_RE.sub("(issue-ref redacted)", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ----------------------------------------------------------------------------
# Static block
# ----------------------------------------------------------------------------

def _static_verdict(row: dict | None) -> tuple[str, str]:
    """Return (verdict, message). Verdict is REJECT/FLAG/PASS/ERROR/MISSING."""
    if row is None:
        return "MISSING", ""
    if row.get("error"):
        return "ERROR", row["error"]
    sub_keys = ("check_patch_applies",)
    sub = [(k, row.get(k, {})) for k in sub_keys]
    if any(s.get("verdict") == "REJECT" for _, s in sub):
        msgs = [f"{k}: {s.get('message','').strip()}"
                for k, s in sub
                if s.get("verdict") == "REJECT" and s.get("message")]
        return "REJECT", "\n".join(msgs) or "static check rejected (no message captured)"
    if any(s.get("verdict") == "FLAG" for _, s in sub):
        return "FLAG", "; ".join(s.get("message", "") for _, s in sub if s.get("verdict") == "FLAG")
    return "PASS", ""


def _static_block(row: dict | None) -> str:
    verdict, msg = _static_verdict(row)
    if verdict == "PASS":
        return "# Static check: PASS"
    if verdict == "MISSING":
        return "# Static check: MISSING (no record for this instance)"
    clipped = _clip(msg, STATIC_MSG_MAX_CHARS)
    header = f"# Static check: {verdict}"
    return f"{header}\n{clipped}" if clipped else header


# ----------------------------------------------------------------------------
# Regression block
# ----------------------------------------------------------------------------

def _regression_block(row: dict | None) -> str:
    if row is None:
        return "# Regression check: MISSING (no record for this instance)"
    verdict = row.get("verdict", "") or "UNKNOWN"
    if row.get("error"):
        return f"# Regression check: ERROR\n{_clip(str(row['error']), STATIC_MSG_MAX_CHARS)}"
    if verdict == "PASS":
        p2p = row.get("p2p_count", 0)
        return f"# Regression check: PASS ({p2p} pre-patch-passing tests re-ran clean on the patched code)"
    if verdict == "SKIP":
        msg = (row.get("message") or "").strip() or "(no skip reason captured)"
        return f"# Regression check: SKIP\n{_clip(msg, STATIC_MSG_MAX_CHARS)}"
    if verdict == "REJECT":
        msg = (row.get("message") or "").strip()
        new_failures = row.get("new_failures") or []
        parts = [f"# Regression check: REJECT"]
        if msg:
            parts.append(_clip(msg, STATIC_MSG_MAX_CHARS))
        if new_failures:
            parts.append("Newly-failing tests (up to 2):")
            for nf in new_failures[:2]:
                if isinstance(nf, dict):
                    name = nf.get("nodeid") or nf.get("name") or "?"
                    tail = (nf.get("stdout_tail") or nf.get("message") or "").strip()
                    parts.append(f"  - {name}")
                    if tail:
                        parts.append(_clip(tail, 600))
                else:
                    parts.append(f"  - {nf}")
        return _clip("\n".join(parts), REGRESSION_MSG_MAX_CHARS)
    return f"# Regression check: {verdict}"


# ----------------------------------------------------------------------------
# Reproduction block
# ----------------------------------------------------------------------------

_BUCKET_RANK = {"A": 0, "B": 1, "C": 2}


def _is_test_path(p: str) -> bool:
    """Heuristic: path looks like a test file (tests/ dir, or test_*.py filename)."""
    low = (p or "").lower().replace("\\", "/")
    if not low:
        return False
    if "/tests/" in low or low.startswith("tests/") or low.startswith("test/") or "/test/" in low:
        return True
    base = low.rsplit("/", 1)[-1]
    return base.startswith("test_") or base.endswith("_test.py") or base == "tests.py"


def _import_path_to_path(import_path: str) -> str | None:
    """Best-effort 'pkg.sub.module.Class' → 'pkg/sub/module.py'. Returns None on failure."""
    if not import_path:
        return None
    parts = [p for p in import_path.split(".") if p and p[:1].islower()]
    if not parts:
        return None
    return "/".join(parts) + ".py"


def _focal_hint(cache: dict | None) -> tuple[list[str], str]:
    """Return (source focal files, focal symbol) from Phase A cache.

    Localize.py sometimes fills focal_files with a test path when the source
    candidate set is empty — for the retry feedback we want to surface the
    *source file where the bug lives*, not the test file, so we filter test
    paths out and fall back to import_path-derived source paths.
    """
    if not cache:
        return [], ""
    loc = cache.get("localization") or {}
    raw = loc.get("focal_files") or ([loc.get("focal_file")] if loc.get("focal_file") else [])
    raw = [f for f in raw if f]
    source = [f for f in raw if not _is_test_path(f)]
    if not source:
        derived = _import_path_to_path(loc.get("import_path") or "")
        if derived and not _is_test_path(derived):
            source = [derived]
    return source, (loc.get("focal_symbol") or "")


def _drop_summary(cache: dict | None) -> list[str]:
    """Summarize why candidate reproduction tests failed the buggy-repo gate."""
    if not cache:
        return []
    drops: Counter = Counter()
    for g in (cache.get("gated_tests") or []):
        if g.get("bucket") == "DROP":
            reason = (g.get("drop_reason") or "unknown")
            # Clean up regex-pattern drop reasons like "\\bImportError\\b" for humans
            readable = reason.replace("\\b", "").strip("\\").strip()
            drops[readable or "unknown"] += 1
    return [f"  - {n} test(s) dropped: {reason}" for reason, n in drops.most_common()]


def _issue_paths(cache: dict | None) -> list[str]:
    """Extract .py file paths mentioned anywhere in the issue text / snippet."""
    if not cache:
        return []
    issue = cache.get("issue") or {}
    text = "\n".join(
        (issue.get(k) or "") for k in ("body", "raw_text", "reporter_snippet", "one_line_summary")
    )
    paths: set[str] = set()
    for m in _PY_PATH_RE.finditer(text):
        p = m.group(1)
        if 4 <= len(p) <= 160 and "/" in p:
            paths.add(p)
    return sorted(paths)[:5]


def _patch_vs_loc_warning(
    patch_files: list[str],
    focal_files: list[str],
    *,
    zero_signal: bool = False,
) -> str | None:
    """Flag the case where the prior patch edits none of the focal files.
    """
    if not patch_files or not focal_files:
        return None
    if set(patch_files) & set(focal_files):
        return None
    if zero_signal:
        return (
            "Patch-vs-localization note (LOW CONFIDENCE): focal analysis suggests "
            "the bug lives in:\n"
            + "\n".join(f"    {f}" for f in focal_files)
            + "\nbut the reproduction gate admitted no tests, so this focal hint is "
              "**uncorroborated** — it may simply be wrong. Your prior patch in:\n"
            + "\n".join(f"    {f}" for f in patch_files)
            + "\nmight already be correct. Prefer MINIMAL refinements (or "
              "no change at all) over rewriting to a different file; only "
              "move if you can articulate a concrete reason the focal file "
              "is right."
        )
    parts = [
        "Patch-vs-localization note: focal-file analysis (from the issue text + "
        "traceback) says the bug lives in:",
        *(f"    {f}" for f in focal_files),
        "Your prior patch touched:",
        *(f"    {f}" for f in patch_files),
        "instead. Before writing another patch in the same file, read at least a "
        "few lines of the focal file and confirm whether it's really the right "
        "place. If you conclude the focal file is genuinely irrelevant, add a "
        "one-line `# NOTE:` in your patch briefly explaining why; otherwise, "
        "edit the focal file.",
    ]
    return "\n".join(parts)


def _load_focal_snippet(iid: str, snippet_dir: Path) -> dict | None:
    p = snippet_dir / f"{iid}.json"
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
    except Exception:
        return None
    if rec.get("status") not in ("ok", "snippet_symbol_not_found"):
        return None
    if rec.get("entries") and any((e.get("snippet") or "").strip() for e in rec["entries"]):
        return rec
    if (rec.get("snippet") or "").strip():
        return rec
    return None


def _focal_entries(snippet: dict | None) -> list[dict]:
    """Return the list of per-file entries from the snippet cache.
    """
    if not snippet:
        return []
    if snippet.get("entries"):
        return [e for e in snippet["entries"] if (e.get("snippet") or "").strip()]
    return [{
        "focal_file": snippet.get("focal_file"),
        "focal_symbol": snippet.get("focal_symbol"),
        "snippet": snippet.get("snippet"),
        "start_line": snippet.get("start_line"),
        "end_line": snippet.get("end_line"),
        "status": snippet.get("status"),
        "symbol_map": [],
    }]


def _effective_focal_files(
    cache: dict | None, snippet: dict | None,
) -> list[str]:
    """Return the focal-file set to surface to the agent.

    Prefer files that the snippet extractor actually located content in.
    Fall back to Phase-A focal_files
    (test-path-filtered) when the snippet cache is missing.
    """
    entries = _focal_entries(snippet)
    if entries:
        # Preserve order; dedupe.
        seen = set()
        out: list[str] = []
        for e in entries:
            f = e.get("focal_file")
            if f and f not in seen:
                seen.add(f)
                out.append(f)
        if out:
            return out
    files, _ = _focal_hint(cache)
    return files


def _render_symbol_map(symbol_map: list[dict]) -> str:
    """render a compact AST symbol listing (hard-capped in chars)."""
    if not symbol_map:
        return ""
    lines: list[str] = []
    for node in symbol_map[:FOCAL_SYMBOL_MAP_MAX_SYMBOLS]:
        kind = node.get("kind", "?")
        name = node.get("name", "?")
        line = node.get("line", "?")
        if kind == "class":
            lines.append(f"  class {name:<30s}(line {line})")
            for child in (node.get("children") or [])[:4]:
                cn = child.get("name", "?"); cl = child.get("line", "?")
                lines.append(f"    def {cn:<30s}(line {cl})")
        elif kind == "func":
            lines.append(f"  def {name:<30s}(line {line})")
    rendered = "\n".join(lines)
    return _clip(rendered, FOCAL_SYMBOL_MAP_MAX_CHARS)


def _focal_source_snippet_block(iid: str, snippet_dir: Path) -> str:
    """up to 3 focal source snippets with AST symbol maps.

    Gracefully returns "" if the cache is missing.
    """
    rec = _load_focal_snippet(iid, snippet_dir)
    if rec is None:
        return ""
    entries = _focal_entries(rec)
    if not entries:
        return ""
    # Divvy the total budget across entries so we don't explode manifest size.
    per_entry_budget = FOCAL_SNIPPET_MAX_CHARS if len(entries) == 1 else max(
        1200, FOCAL_SNIPPET_MAX_CHARS // len(entries)
    )
    parts: list[str] = []
    for idx, e in enumerate(entries[:FOCAL_SNIPPET_MAX_ENTRIES], start=1):
        focal_file = e.get("focal_file") or "(unknown)"
        symbol = e.get("focal_symbol") or ""
        start = e.get("start_line")
        end = e.get("end_line")
        snippet = _clip(e.get("snippet") or "", per_entry_budget)
        header_sym = f"around `{symbol}`" if symbol else "(file head)"
        tag = f" [focal #{idx} of {len(entries)}]" if len(entries) > 1 else ""
        caveat = ""
        if e.get("status") == "snippet_symbol_not_found":
            caveat = (
                "(symbol `{s}` not found at this path; showing file head. The "
                "symbol may live in a different file; take this as a low-"
                "confidence hint.)"
            ).format(s=symbol) if symbol else ""
        parts.append(
            f"# Focal file current source{tag} "
            f"({focal_file}, lines {start}-{end}, {header_sym}):"
        )
        parts.append("```python")
        parts.append(snippet)
        parts.append("```")
        if caveat:
            parts.append(caveat)
        sym_block = _render_symbol_map(e.get("symbol_map") or [])
        if sym_block:
            parts.append(f"Symbols defined in {focal_file}:")
            parts.append(sym_block)
    return "\n".join(parts)


def _multi_file_hint(cache: dict | None, *, has_bucket_a: bool) -> str:
    """multi-file hint gated on cascade bucket-A admission.

    """
    if not has_bucket_a:
        return ""
    paths = _issue_paths(cache)
    if len(paths) < MULTI_FILE_HINT_MIN_PATHS:
        return ""
    lines = [
        "# Multi-file-fix hint:",
        f"The issue / hints reference {len(paths)} distinct code locations: "
        + ", ".join(f"`{p}`" for p in paths[:5]) + ".",
        "Gold-quality fixes often span multiple files when the bug involves "
        "an API signature change or data flow across modules. Before submitting "
        "a single-file patch, verify that no other mentioned file needs a "
        "matching change.",
    ]
    return "\n".join(lines)


def _pick_top_failing(per_test: list[dict]) -> dict | None:
    failing = [pt for pt in per_test if not pt.get("passed", True)]
    if not failing:
        return None
    failing.sort(
        key=lambda pt: (-float(pt.get("weight", 0.0)), _BUCKET_RANK.get(pt.get("bucket", "C"), 9))
    )
    return failing[0]


def _clip_lines(text: str, max_lines: int) -> str:
    """Keep only the first `max_lines` of `text`, with a truncation marker."""
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text.rstrip()
    kept = lines[:max_lines]
    kept.append(f"# ... [truncated at {max_lines} lines; full source is ~{len(lines)} lines]")
    return "\n".join(kept)


def _gated_test_source(cache: dict | None, cand_id: str) -> str | None:
    """Fetch the source of a gated test by cand_id from the Phase-A cache."""
    if not cache or not cand_id:
        return None
    for g in (cache.get("gated_tests") or []):
        if g.get("cand_id") == cand_id:
            return g.get("source") or None
    # Fallback: the cand_id might correspond to a DROPped candidate only present
    # in candidates[] (not gated_tests[]). Surface it anyway — the agent can still
    # reason about what was attempted.
    for c in (cache.get("candidates") or []):
        if c.get("cand_id") == cand_id:
            return c.get("source") or None
    return None


def _synth_feedback_block(row: dict | None) -> str:
    """Render the LLM-synthesized reviewer notes.

    Only render synth when the reproduction signal is
    strong enough to be trusted:
      - cascade verdict is FAIL
      - AND ≥2 counted failing per-tests in bucket A (weight 1.0)
    """
    if not row:
        return ""
    detail = row.get("feedback_detail") or {}
    scenario = (detail.get("scenario") or "").strip()
    gap = (detail.get("conceptual_gap") or "").strip()
    direction = (detail.get("direction") or "").strip()
    flat_feedback = (row.get("feedback") or "").strip()
    have_structured = any((scenario, gap, direction))
    if not have_structured and not flat_feedback:
        return ""

    # v9.1 narrow gate: cascade verdict FAIL AND ≥2 bucket-A counted failing
    if row.get("verdict") != "FAIL":
        return ""
    a_failures = sum(
        1 for pt in (row.get("per_test") or [])
        if pt.get("counted", True)
        and not pt.get("passed", True)
        and pt.get("bucket") == "A"
    )
    if a_failures < 2:
        return ""

    lines = [
        "Reviewer notes (LLM-synthesized from your patch + the failing test — "
        "may miss nuance; the test source above is ground truth, treat these "
        "notes as a second opinion):",
    ]
    if have_structured:
        if scenario:
            lines.append(f"- Scenario: {_clip(scenario, 400)}")
        if gap:
            lines.append(f"- Conceptual gap: {_clip(gap, 400)}")
        if direction:
            lines.append(f"- Direction for the next attempt: {_clip(direction, 400)}")
    else:
        # Flat-feedback fallback (defensive — kept from v14).
        lines.append(_clip(flat_feedback, 1200))
    return "\n".join(lines)


def _repro_test_source_block(cand_id: str, cache: dict | None) -> str:
    """Render the highest-weight failing test's source code (v8 C1).

    Clip by lines first (readability), then by chars (hard cap). Skip
    gracefully if we can't locate the source.
    """
    src = _gated_test_source(cache, cand_id)
    if not src or not src.strip():
        return ""
    clipped = _clip_lines(src, REPRO_TEST_SOURCE_MAX_LINES)
    clipped = _clip(clipped, REPRO_TEST_SOURCE_MAX_CHARS)
    return (
        "Failing reproduction test source (shows what this test actually asserts — "
        "adapt your patch to satisfy these asserts, not just the stdout above):\n"
        "```python\n"
        f"{clipped}\n"
        "```"
    )


def _issue_expectation_block(cache: dict | None) -> str:
    """Render the issue's parsed expected/actual fields as a named block (v8 C2).

    The IssueBundle parser already extracts these from the bug report; today
    they're buried inside the prior-patch's rendered `issue_body`. Surface
    them as a named block so the agent has a clean anchor on "what the bug
    report says the correct behavior is".
    """
    if not cache:
        return ""
    issue = cache.get("issue") or {}
    expected = (issue.get("expected") or "").strip()
    actual = (issue.get("actual") or "").strip()
    # Treat "(unknown)" or empty as absent.
    if not expected or expected.lower() == "(unknown)":
        expected = ""
    if not actual or actual.lower() == "(unknown)":
        actual = ""
    if not expected and not actual:
        return ""
    lines = ["# Issue expectation (parsed from the bug report):"]
    if expected:
        lines.append(f"- Expected: {_clip(expected, 200)}")
    if actual:
        lines.append(f"- Actual:   {_clip(actual, 200)}")
    return "\n".join(lines)


def _count_patch_lines(patch: str) -> int:
    """Count added + removed lines in a unified diff, excluding hunk headers."""
    if not patch:
        return 0
    n = 0
    for line in patch.splitlines():
        if not line:
            continue
        if line.startswith(("+++", "---", "@@")):
            continue
        if line[0] in "+-":
            n += 1
    return n


def _patch_size_note(patch: str) -> str:
    """Render a soft proportionality nudge for oversized first-run patches.

    Returns empty string when the patch is under the threshold.
    """
    n = _count_patch_lines(patch)
    if n <= OVERSIZED_PATCH_LINE_THRESHOLD:
        return ""
    return (
        f"# NOTE: your previous patch had {n} added/removed lines — real bugfix "
        f"patches in this roster are typically much smaller. If you can't justify "
        f"every hunk as targeting the issue, tighten scope rather than expanding."
    )


def _patch_vs_loc_block(
    prior_patch_files: list[str],
    effective_focal_files: list[str],
    *,
    zero_signal: bool,
) -> str:
    """wrapper around `_patch_vs_loc_warning` so it can be assembled
    in the localization section rather than buried inside the reproduction
    block.
    """
    warn = _patch_vs_loc_warning(
        prior_patch_files or [], effective_focal_files or [],
        zero_signal=zero_signal,
    )
    return warn or ""


def _repro_block(
    row: dict | None,
    cache: dict | None,
    *,
    prior_patch_files: list[str] | None = None,
    effective_focal_files: list[str] | None = None,
) -> str:
    if row is None:
        return "# Reproduction check: MISSING (no record for this instance)"
    verdict = row.get("verdict", "") or "UNKNOWN"

    # A1 honest fallback — no synthesized narrative for zero-signal, but we DO
    # now surface localization hints + drop-reason counts + issue-mentioned paths
    # so the retry solver is not flying blind.  Still no commitment to a verdict.
    if verdict == "UNCERTAIN_ZERO_SIGNAL":
        drop_lines = _drop_summary(cache)
        parts = [
            "# Reproduction check: UNCERTAIN_ZERO_SIGNAL",
            (
                "No reproduction test survived the Phase-A admission gate (no verified "
                "reproduction is available for this instance). Treat this retry as an "
                "open re-investigation: do not assume the prior patch's framing of the "
                "problem is correct — but also note cascade has zero concrete evidence "
                "the prior patch is *wrong*, so prefer minimal changes when in doubt."
            ),
        ]
        if drop_lines:
            parts.append("")
            parts.append("Why no verified reproduction test survived the gate:")
            parts.extend(drop_lines)
        return "\n".join(parts)

    if row.get("error"):
        return f"# Reproduction check: ERROR\n{_clip(str(row['error']), STATIC_MSG_MAX_CHARS)}"

    if verdict == "PASS":
        score = row.get("score", 0.0)
        buckets = row.get("buckets_used", {}) or {}
        return (
            f"# Reproduction check: PASS (score={score:.2f}, "
            f"buckets_used={dict(buckets)})"
        )

    per_test = row.get("per_test", []) or []
    top = _pick_top_failing(per_test)

    if verdict == "FAIL":
        score = float(row.get("score", 0.0))
        n_counted = sum(
            1 for pt in per_test
            if pt.get("counted", True) and float(pt.get("weight", 0.0)) > 0
        )
        header = (
            f"# Reproduction check: FAIL (score={score:.2f}, "
            f"counted_tests={n_counted})"
        )
        if top is None:
            return f"{header}\n(FAIL reported but no per-test evidence captured.)"
        cand_id = top.get("cand_id", "?")
        bucket = top.get("bucket", "?")
        weight = top.get("weight", 0.0)
        marker = top.get("marker") or "(none)"
        exit_code = top.get("exit_code")
        stdout_tail = _clip(top.get("stdout_tail") or "", REPRO_STDOUT_MAX_CHARS)

        parts = [header]
        parts.append(
            f"Highest-weight failing reproduction test: {cand_id} "
            f"(bucket={bucket}, weight={weight}, exit_code={exit_code}, marker={marker})."
        )
        if stdout_tail:
            parts.append("Observed stdout tail on your patched code:")
            parts.append("```")
            parts.append(stdout_tail)
            parts.append("```")
        src_block = _repro_test_source_block(cand_id, cache)
        if src_block:
            parts.append("")
            parts.append(src_block)
        synth_block = _synth_feedback_block(row)
        if synth_block:
            parts.append("")
            parts.append(synth_block)
        return "\n".join(parts)

    if verdict == "UNCERTAIN":
        score = row.get("score", 0.0)
        parts = [f"# Reproduction check: UNCERTAIN (score={score:.2f}; mixed signal)"]
        if top is not None:
            cand_id = top.get("cand_id", "?")
            stdout_tail = _clip(top.get("stdout_tail") or "", REPRO_STDOUT_MAX_CHARS)
            parts.append(
                f"Most diagnostic failing test: {cand_id} "
                f"(bucket={top.get('bucket')}, weight={top.get('weight')})."
            )
            if stdout_tail:
                parts.append("Observed stdout tail:")
                parts.append("```")
                parts.append(stdout_tail)
                parts.append("```")
            src_block = _repro_test_source_block(cand_id, cache)
            if src_block:
                parts.append("")
                parts.append(src_block)
        synth_block = _synth_feedback_block(row)
        if synth_block:
            parts.append("")
            parts.append(synth_block)
        return "\n".join(parts)

    return f"# Reproduction check: {verdict}"


# ----------------------------------------------------------------------------
# Judge-rubric block  (Proposal 2 — LLM meta-signal with hallucination caveat)
# ----------------------------------------------------------------------------

_RUBRIC_LABELS = {
    "root_cause": "patch targets the right region",
    "not_symptom_only": "patch fixes the bug, not just its symptom",
    "no_scope_creep": "patch stays within scope",
    "failing_tests_legitimate": "failing repro tests actually test this bug",
}


def _judge_block(row: dict | None) -> str:
    """Render a compact, hedged summary of the Layer-3 judge verdict.

    Design:
      - Render rubric scores only (never the judge's free-form reasoning).
      - Wrap in an explicit hallucination caveat: this is an LLM's opinion, not
        a verified result.  The caveat points the solver back at the raw repro
        block above when the judge disagrees with it.
      - If the judge row is missing (no judge call) or failed to parse, say so
        rather than omit — the *fact* that a judge slot exists but is empty is
        itself a signal.
    """
    if row is None:
        # No judge ever ran for this instance (repro decided PASS/FAIL directly,
        # or cascade-cut before the judge).  Omit the block entirely.
        return ""
    judge = row.get("judge") or {}
    # Handle row-level error (validate_judge caught an exception before the judge ran)
    if "error" in row and not judge:
        return (
            "# Cascade-judge rubric (LLM-generated — may be wrong or hallucinated):\n"
            f"unavailable (judge errored: {_clip(str(row['error']), 120)})"
        )
    parse_error = judge.get("parse_error")
    verdict = judge.get("verdict", "UNKNOWN")
    confidence = judge.get("confidence", "?")
    rubric = judge.get("rubric_scores") or {}

    parts = [
        "# Cascade-judge rubric (LLM-generated — may be wrong or hallucinated):"
    ]
    if parse_error and not rubric:
        parts.append(f"unavailable (parse_error: {parse_error}; verdict={verdict})")
    else:
        parts.append(
            "An LLM reviewed your prior patch against the issue and the verified"
            " reproduction tests, and scored it on four yes/no rubric items"
            " (1 = satisfies, 0 = fails):"
        )
        for k, label in _RUBRIC_LABELS.items():
            if k not in rubric:
                continue
            score = rubric[k]
            parts.append(f"  - {k:<26s} {score}   ({label})")
        parts.append(
            f"Overall judge verdict: {verdict}, confidence={confidence}"
        )

    parts.append("")
    parts.append(
        "IMPORTANT: this block is an LLM's opinion, not a verified result. It"
        " can be wrong in two specific ways that matter here:"
    )
    parts.append(
        "  1. The judge may rate your patch highly while the reproduction block"
        " above still shows failing tests. In that case the judge may be"
        " over-trusting your diff — weigh it against the raw test output, not"
        " instead of it."
    )
    parts.append(
        "  2. The judge may flag the failing reproduction tests as not"
        " legitimate. If it does, that is a hint — not a license to ignore the"
        " failure. Read the test source above and decide for yourself whether"
        " the test is actually probing the bug described in the issue."
    )
    return _clip("\n".join(parts), JUDGE_BLOCK_MAX_CHARS)


# ----------------------------------------------------------------------------
# Prior-patch block
# ----------------------------------------------------------------------------

def _prior_patch_block(iid: str, preds: dict) -> str:
    entry = preds.get(iid) or {}
    patch = entry.get("model_patch", "") or ""
    if not patch.strip():
        return (
            "# Your previous patch (first-run attempt):\n"
            "(no patch was recorded for this instance)"
        )
    cleaned = _strip_patch_metadata(patch)
    clipped = _clip(cleaned, PRIOR_PATCH_MAX_CHARS)
    size_note = _patch_size_note(cleaned)
    out = (
        "# Your previous patch (first-run attempt):\n"
        "```diff\n"
        f"{clipped}\n"
        "```"
    )
    if size_note:
        out += "\n" + size_note
    return out


# ----------------------------------------------------------------------------
# Cascade decision + block assembly
# ----------------------------------------------------------------------------

def _cascade_final(
    iid: str,
    *,
    static: dict,
    dynreg: dict,
    repro: dict,
    judge: dict,
) -> tuple[str, str]:
    """Return (final_verdict, rejecting_layer) — the label used for selection
    and summary only.  Feedback text is assembled separately and deterministically.
    """
    s_verdict, _ = _static_verdict(static.get(iid))
    if s_verdict == "REJECT":
        return "FAIL", "static"
    if s_verdict == "ERROR":
        return "ERROR", "static"

    d_row = dynreg.get(iid) or {}
    d_verdict = d_row.get("verdict")
    if d_verdict == "REJECT":
        return "FAIL", "dynamic_regression"
    if d_verdict == "ERROR" or d_row.get("error"):
        return "ERROR", "dynamic_regression"

    r_row = repro.get(iid) or {}
    r_verdict = r_row.get("verdict", "")
    if r_verdict == "PASS":
        return "PASS", ""
    if r_verdict == "FAIL":
        return "FAIL", "reproduction"
    if r_verdict in ("UNCERTAIN", "UNCERTAIN_ZERO_SIGNAL"):
        j_row = judge.get(iid)
        if j_row is None:
            return "UNCERTAIN", "reproduction"
        if "error" in j_row and "judge" not in j_row:
            return "ERROR", "judge"
        final = (j_row.get("final") or {}).get("verdict", "UNCERTAIN")
        return final, "judge"
    if r_verdict == "ERROR":
        return "ERROR", "reproduction"
    return "MISSING", ""


def assemble_feedback(
    iid: str,
    *,
    static: dict,
    dynreg: dict,
    repro: dict,
    judge: dict,
    preds: dict,
    cache_dir: Path,
    include_judge: bool = True,
    focal_snippets_dir: Path | None = None,
) -> str:
    cache = _load_phase_a(cache_dir, iid)
    prior_patch_files = _patch_files((preds.get(iid) or {}).get("model_patch") or "")
    snippet_dir = focal_snippets_dir or DEFAULT_FOCAL_SNIPPETS
    focal_snippet = _load_focal_snippet(iid, snippet_dir)
    eff_focal = _effective_focal_files(cache, focal_snippet)

    repro_row = repro.get(iid) or {}
    repro_verdict = repro_row.get("verdict", "")
    is_zero_signal = repro_verdict == "UNCERTAIN_ZERO_SIGNAL"
    has_bucket_a = bool((repro_row.get("buckets_used") or {}).get("A", 0))


    blocks = [
        _issue_expectation_block(cache),
        _multi_file_hint(cache, has_bucket_a=has_bucket_a),
        _focal_source_snippet_block(iid, snippet_dir),
        _patch_vs_loc_block(
            prior_patch_files, eff_focal,
            zero_signal=is_zero_signal,
        ),
        _prior_patch_block(iid, preds),
        _static_block(static.get(iid)),
        _regression_block(dynreg.get(iid)),
        _repro_block(
            repro_row, cache,
            prior_patch_files=prior_patch_files,
            effective_focal_files=eff_focal,
        ),
    ]
    if include_judge:
        # Judge block follows reproduction so its "reproduction block above"
        # self-reference makes sense in reading order.
        blocks.append(_judge_block(judge.get(iid)))
    joined = "\n\n".join(b.strip() for b in blocks if b)
    return _clip(joined, MAX_FEEDBACK_CHARS)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--static", type=Path, default=DEFAULT_STATIC)
    p.add_argument("--dynreg", type=Path, default=DEFAULT_DYNREG)
    p.add_argument("--repro", type=Path, default=DEFAULT_REPRO)
    p.add_argument("--judge", type=Path, default=DEFAULT_JUDGE,
                   help="Judge results (used for cascade verdict only; judge "
                        "text is not included in the feedback payload).")
    p.add_argument("--preds", type=Path, default=DEFAULT_PREDS,
                   help="First-run preds.json — source of the prior-patch block.")
    p.add_argument("--phase-a-cache", type=Path, default=DEFAULT_PHASE_A_CACHE,
                   help="Directory holding per-instance Phase-A cache JSONs "
                        "(used to pull gated-test source for the repro block).")
    p.add_argument("--focal-snippets-dir", type=Path, default=DEFAULT_FOCAL_SNIPPETS,
                   help="Directory with per-instance focal-source-snippet caches "
                        "(produced by scripts/extract_focal_snippets.py; v10 A).")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--no-judge-block", action="store_true",
                   help="Omit the Proposal-2 judge-rubric block (regenerate the"
                        " original 4-block manifest for A/B testing).")
    args = p.parse_args()

    static = _index(args.static)
    dynreg = _index(args.dynreg)
    repro = _index(args.repro)
    judge = _index(args.judge)
    preds = _load_preds(args.preds)

    # Iterate over preds.keys() so that harness-error / not-validated instances
    # (which have no row in any results.json) reach _cascade_final and surface as
    # MISSING — those need to be retried too under the consolidated rule.
    ids = list(preds.keys()) or list(repro.keys()) or list(static.keys())

    manifest: dict[str, str] = {}
    rows: list[tuple[str, str, str, int]] = []  # (iid, final, layer, chars)

    for iid in ids:
        final, layer = _cascade_final(
            iid, static=static, dynreg=dynreg, repro=repro, judge=judge,
        )
        # Retry on anything that isn't a clean PASS.
        if final == "PASS":
            continue
        text = assemble_feedback(
            iid,
            static=static,
            dynreg=dynreg,
            repro=repro,
            judge=judge,
            preds=preds,
            cache_dir=args.phase_a_cache,
            include_judge=not args.no_judge_block,
            focal_snippets_dir=args.focal_snippets_dir,
        )
        manifest[iid] = text
        rows.append((iid, final, layer, len(text)))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "feedback_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    # Build anchored regex for --filter
    escaped = sorted(re.escape(iid) for iid in manifest)
    regex = "^(?:" + "|".join(escaped) + ")$" if escaped else "^$"
    (args.output_dir / "retry_filter.txt").write_text(regex)

    # Print summary
    print(f"{'instance_id':<48} {'final':<10} {'layer':<20} chars")
    print("-" * 90)
    for iid, final, layer, n in sorted(rows):
        print(f"{iid:<48} {final:<10} {layer:<20} {n}")

    layer_counts = Counter(layer for _, _, layer, _ in rows)
    final_counts = Counter(final for _, final, _, _ in rows)
    print()
    print(f"Total entries        : {len(manifest)}")
    print(f"By final verdict     : {dict(final_counts)}")
    print(f"By rejecting layer   : {dict(layer_counts)}")
    if manifest:
        lengths = [len(v) for v in manifest.values()]
        print(f"Avg feedback length  : {sum(lengths)/len(lengths):.0f} chars")
        lengths.sort()
        p95 = lengths[int(0.95 * (len(lengths) - 1))]
        print(f"P95 feedback length  : {p95} chars")
        print(f"Max feedback length  : {max(lengths)} chars")
    print(f"\nManifest : {manifest_path}")
    print(f"Filter   : {args.output_dir / 'retry_filter.txt'}")


if __name__ == "__main__":
    main()

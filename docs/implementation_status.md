# SIEVE — Layer 1 Static Checker: Implementation Status

> Last updated: 2026-04-16

---

## Evaluation Trajectory

All results are measured against the **50-instance Gemini 2.5 Pro pilot set**.

| File | Path |
|---|---|
| Predictions | `runs/swe-verified_50_gemini-2.5-pro/swe_verified_50_gemini-2.5-pro-new/preds.json` |
| Ground truth | `runs/sb-cli-reports/gemini__gemini-2.5-pro.gemini-2.5-pro-mini50-run.json` |
| Static checks validation script | `scripts/validate_static_checks.py` |
| Semgrep validation script | `scripts/validate_semgrep.py` |
| Static checks results | `runs/static_checks_validation/results.json` |
| Semgrep results (post-only, old) | `runs/semgrep_validation/summary.json` |

**Ground-truth breakdown:**

| Category | Count | Notes |
|---|---|---|
| Resolved (correct patch) | 26 | SWE-bench harness: PASS |
| Unresolved (incorrect patch) | 19 | SWE-bench harness: FAIL |
| Error (harness could not evaluate) | 5 | Excluded from all evaluations |
| **Total** | **50** | Django + Sphinx, SWE-bench Verified split |

Error instances excluded from every checker evaluation:
`django__django-12325`, `sphinx-doc__sphinx-8056`, `sphinx-doc__sphinx-8265`,
`sphinx-doc__sphinx-9229`, `sphinx-doc__sphinx-9230`.

**Validation methodology:** Each checker is exercised via `scripts/validate_static_checks.py`,
which starts a fresh per-instance SWE-bench Docker container (`swebench/sweb.eval.x86_64.<id>:latest`),
copies the patch in, and runs the checks in pipeline order. Containers are torn down after
each instance to prevent state leakage.

---

## Pipeline Overview

All four checkers are wired together by `run_static_checks()` in
`sieve/phases/static.py`. The orchestrator applies the patch **once** and
passes pre- and post-patch tool outputs to the individual delta-computation
steps, avoiding the double-apply problem that would arise if each checker
applied the patch independently.

```
run_static_checks(repo_path, patch_file, config="p/python")
│
├─ Step 1  check_patch_applies      git apply --check (dry-run, no disk change)
│          ↓ REJECT → stop early     ↓ PASS → continue
│
├─ Step 2  pre-patch baselines
│          _run_flake8_json(py_files)   → pre_lint   (set of tuples)
│          _run_semgrep_json(py_files)  → pre_semgrep (list of dicts)
│
├─ Step 3  git apply patch_file          ← single patch apply, owned by orchestrator
│          ↓ non-zero → REJECT + stop
│
├─ Step 4  check_files_parse        ast.parse + py_compile on post-patch .py files
│          ↓ REJECT → stop early     ↓ PASS → continue
│
├─ Step 5  lint delta
│          post_lint = _run_flake8_json(py_files)
│          new = sorted(post_lint − pre_lint)
│          → REJECT (E9* / reject-codes) | FLAG (flag-codes) | PASS
│
├─ Step 6  semgrep delta
│          post_semgrep = _run_semgrep_json(py_files)
│          new = [r for r in post_semgrep if fingerprint(r) not in pre_fps]
│          → FLAG (any new finding) | PASS
│
└─ Step 7  aggregate → StaticCheckResult
           verdict  = REJECT if any check REJECTed
                    = FLAG   if any check FLAGged (no REJECT)
                    = PASS   otherwise
```

**Early-stop policy:** Steps 1 and 4 are hard gates. A REJECT at either point
skips all remaining steps — there is no value in running slower tools (flake8,
semgrep) against a patch that cannot even be applied or parsed.

**Single-apply design:** `check_lint_delta` and `check_semgrep(patch_file=…)`
each apply the patch internally when called as standalone functions. Inside
the orchestrator both are replaced by their underlying private helpers
(`_run_flake8_json`, `_run_semgrep_json`), so the patch is applied exactly
once in Step 3. The standalone functions remain available for scripts that
need to run one check in isolation.

**Return type:** `StaticCheckResult` dataclass with fields:

```python
@dataclass
class StaticCheckResult:
    verdict:        str                   # "PASS" | "REJECT" | "FLAG"
    checks_passed:  list[str]             # check_names that returned PASS
    checks_failed:  list[str]             # check_names that returned REJECT
    flags:          list[str]             # FLAG messages (non-blocking)
    details:        dict[str, CheckResult]  # full per-check result
```

---

## Step 1 — Patch Applicability (`check_patch_applies`)

**What it does:** Runs `git apply --check <patch_file>` inside the repository
root. The `--check` flag performs a dry-run — it validates that the patch
hunks match the current file content without modifying any file on disk.
Returns REJECT with git's full stderr on any non-zero exit code.

**What it catches:**
- Patches truncated by the LLM mid-output (common when the response hits a
  token or safety limit)
- Patches targeting a file path that does not exist in the repository
  (hallucinated path, wrong version)
- Patches generated in a non-standard diff format that `git apply` cannot
  parse (e.g., diffs generated against a `.orig` backup file rather than the
  actual source)

**Implementation notes:**
- Runs as a pure dry-run; does not touch the working tree
- Returns the raw `git` stderr in `CheckResult.message` for diagnostics
- Acts as the first hard gate: a REJECT here skips all subsequent checks

**Pilot results (45 evaluable instances):**

| Outcome | Count | Instances | Ground truth |
|---|---|---|---|
| REJECT — non-standard diff format | 1 | `sphinx-doc__sphinx-7757` | unresolved |
| PASS | 44 | — | — |

`sphinx-doc__sphinx-7757` uses a diff generated against a `.orig` backup
(`sphinx/util/inspect.py.orig`) rather than the actual source file. Git
cannot locate that path in the repository and rejects the patch outright.

**Confusion matrix (45 evaluable instances):**

| | Resolved (26) | Unresolved (19) |
|---|---|---|
| REJECT | 0 FP | 1 TP |
| PASS | 26 TN | 18 FN |

**Hard FP rate: 0 / 26 = 0%.  Catch rate: 1 / 19 = 5%.**

---

## Step 4 — Syntax Check (`check_files_parse`)

**What it does:** For every `.py` file touched by the patch (post-apply),
runs two syntax checks in sequence:

1. **`ast.parse()`** — Python's built-in parser; gives a precise
   `file:lineno: message` error string and is the primary rejection signal.
2. **`python -m py_compile`** — secondary pass that catches encoding and codec
   errors that `ast.parse` occasionally misses.

Non-Python files (`.rst`, `.html`, `.c`, etc.) and files deleted by the patch
are silently skipped. Returns REJECT on the first file that fails either check.

**What it catches:** Patches that introduce a Python `SyntaxError` — for
example, inserting an import above `from __future__ import annotations` (which
Python forbids), or LLM output that contains a partially written function
definition.

**Implementation notes:**
- Runs after the patch has been applied (Step 3), reading files from disk
- Acts as the second hard gate: a REJECT skips lint and semgrep steps
- `ast.parse()` is preferred for error messages; `py_compile` is a backstop
  for encoding-level issues that the AST parser doesn't always surface

**Pilot results (44 instances passing Step 1):**

| Outcome | Count | Instance | Error |
|---|---|---|---|
| REJECT — `SyntaxError` | 1 | `sphinx-doc__sphinx-11510` | `from docutils import …` placed before `from __future__ import annotations` |
| SKIP | 1 | `sphinx-doc__sphinx-7757` | Patch did not apply (Step 1 REJECT) |
| PASS | 43 | — | — |

**Confusion matrix (44 evaluable, 1 skipped):**

| | Resolved (26) | Unresolved (18 evaluable) |
|---|---|---|
| REJECT | 0 FP | 1 TP |
| PASS | 26 TN | 17 FN |

**Hard FP rate: 0 / 26 = 0%.  Catch rate: 1 / 18 = 6%.**

---

## Step 5 — Lint Delta (`check_lint_delta`)

**What it does:** Runs `flake8 --select=F,E9 --format=json` on the modified
`.py` files **before** and **after** applying the patch, then reports only
diagnostics that appear in the post-patch output but not in the pre-patch
baseline (set subtraction keyed on `(code, path, line, col, text)`).

Pre-existing flake8 findings in the same file are subtracted out and never
surfaced. This is the key property that keeps false-positive rate at zero even
in files that already have lint issues.

**Verdict mapping:**

| Code category | Verdict | Rationale |
|---|---|---|
| `E9*` (syntax errors at runtime) | REJECT | Will crash at import time |
| `F404`, `F821–F831`, `F9*`, format codes | REJECT | Undefined names, future-import ordering |
| `F401`, `F811`, `F841`, `F842`, `F631`, etc. | FLAG | Suspicious but non-fatal |
| `E501`, `F402`, `F403`, `F406`, `F407` | ignored | Line length, star imports — too noisy |

**Implementation notes:**
- Delta mode: runs flake8 before applying patch, then again after; only new
  findings (absent from the pre-patch baseline) are reported
- The set subtraction key includes `(code, path, line, col, text)` — all five
  fields must match for a finding to be considered "pre-existing"
- **Known limitation — line-number shifting:** When a patch inserts or removes
  lines, pre-existing findings shift to new line numbers and can appear "new"
  in the delta. In the pilot corpus the files are clean enough that this did
  not trigger any false positives.

**Pilot results (Docker validation — 44 instances passing Step 1):**

| Outcome | Count | Instance | Code | Notes |
|---|---|---|---|---|
| REJECT | 1 | `sphinx-doc__sphinx-11510` | F404 | `from __future__` placed after other imports |
| FLAG | 1 | `django__django-11790` | F811 | Redefinition of unused `__init__` from line 183 |
| PASS | 42 | — | — | — |
| SKIP | 1 | `sphinx-doc__sphinx-7757` | — | Patch did not apply (Step 1 REJECT) |

`sphinx-doc__sphinx-11510` is caught by both Step 4 (`check_files_parse`) and
Step 5 (`check_lint_delta`) independently — the F404 from flake8 flags the same
`from __future__` ordering violation that the AST parser also rejects.

`django__django-11790` is the only catch that is **unique to `check_lint_delta`**:
the patch redefines `__init__` without removing the original definition (F811),
which is syntactically valid and therefore invisible to `check_files_parse`.

> **Note on Docker setup:** SWE-bench eval images do not pre-install `flake8`.
> The validation script installs it at runtime via
> `pip install flake8 flake8-json --quiet` inside each container before running
> checks (`_install_flake8()` helper, called once per container after patch copy).

**Confusion matrix (44 evaluable, 1 skipped):**

| | Resolved (26) | Unresolved (18 evaluable) |
|---|---|---|
| REJECT | 0 FP | 1 TP (`sphinx-11510`) |
| FLAG | 0 soft FP | 1 soft TP (`django-11790`) |
| PASS | 26 TN | 16 FN |

**Hard FP rate: 0 / 26 = 0%.  Catch rate (REJECT+FLAG): 2 / 18 = 11%.**

---

## Step 6 — Semgrep Delta (`check_semgrep`)

**What it does:** Runs `semgrep --config p/python --json --quiet` on the
modified `.py` files before and after the patch, then reports only findings
whose fingerprint `(rule_id, path, start_line)` is absent from the pre-patch
baseline. All findings are reported as FLAG — semgrep results alone never
produce REJECT.

**What it catches:** Security anti-patterns and code quality issues introduced
by the patch — for example, `subprocess.run(shell=True)`, open-redirect
patterns, unsafe use of `eval`, bare `except:` clauses flagged by relevant
rules.

**Implementation notes:**
- Delta mode only: fingerprint = `(rule_id, path, start_line)`. The message
  text is intentionally excluded from the fingerprint because it can change
  when surrounding context shifts after line insertions/deletions.
- Execution failure (missing binary, network error, semgrep crash) returns
  PASS with a warning logged — a broken semgrep installation never blocks the
  pipeline.
- `p/python` covers ~150 rules targeting security patterns and code smells.
  It does **not** detect algorithmic or semantic correctness errors, which
  accounts for the high false-negative rate.

**Why delta mode is necessary — post-only mode analysis:**

Running semgrep only on post-patch files (old approach) produced 2 false
positives on resolved patches:

| Instance | Patch changes | What semgrep flagged | Root cause |
|---|---|---|---|
| `django__django-12143` | `_get_edited_object_pks` (~line 1631): adds `re.escape(prefix)` | Open-redirect at lines 1223, 1273 | Pre-existing pattern in `options.py`, unrelated to patch |
| `django__django-12713` | `formfield_for_manytomanyfield` (~line 249): adds guard | Open-redirect at lines 1223, 1273 | Same pre-existing pattern in same file |

Both patches touch `django/contrib/admin/options.py`, which contains
pre-existing open-redirect findings at lines 1223/1273 completely unrelated
to either change. Post-only mode surfaced those as new findings; delta mode
subtracts them via fingerprint and returns PASS for both.

**Pilot results — post-only mode (superseded):**

| Outcome | Count | Ground truth |
|---|---|---|
| FLAG | 2 | both resolved — false positives |
| PASS | 42 | — |
| Error (patch unapplicable) | 1 | `sphinx-doc__sphinx-7757` |

FP rate: **7.7% (2/26).**

**Pilot results — delta mode (current):**

Both false positives are eliminated. The open-redirect findings at lines
1223/1273 exist identically in the pre-patch baseline; the delta subtraction
removes them from the reported set, returning PASS for both instances.

| Outcome | Count | Notes |
|---|---|---|
| FLAG | 0 | No new findings introduced by any patch |
| PASS | 44 | — |
| Error | 1 | `sphinx-doc__sphinx-7757` (patch unapplicable, Step 1 stops first) |

**FP rate: 0 / 26 = 0%.  Catch rate: 0 / 19 = 0%.**

**On the 0% catch rate:** All 19 incorrect patches in this pilot are
semantically wrong fixes — wrong condition, wrong variable, incomplete logic,
wrong return type — that are syntactically and structurally clean.
`p/python` detects security anti-patterns and code smells, not algorithmic
errors. This high false-negative rate is expected and motivates Phase 3
(custom rule evolution from failure analysis on the 19 unresolved patches).

---

## Combined Results — All 45 Evaluable Instances

The table below shows what each step uniquely adds, in pipeline order.
"Catches" = bad patches (unresolved) given a non-PASS verdict.
"FP" = resolved patches given a non-PASS verdict.

| Step | Check | Catches | FP | Hard FP rate | Catch rate |
|---|---|---|---|---|---|
| 1 | `check_patch_applies` | 1 REJECT | 0 | 0 / 26 = 0% | 1 / 19 = 5% |
| 4 | `check_files_parse` | 1 REJECT | 0 | 0 / 26 = 0% | 1 / 18† = 6% |
| 5 | `check_lint_delta` | 1 REJECT + 1 FLAG | 0 | 0 / 26 = 0% | 2 / 18† = 11% |
| 6 | `check_semgrep` (post-only, old) | 0 | 2 FLAG | 2 / 26 = **7.7%** | — |
| 6 | `check_semgrep` (delta, current) | 0 | 0 | **0 / 26 = 0%** | 0 / 19 = 0% |
| **Pipeline total (delta mode)** | | **3 unique catches** | **0** | **0%** | **3 / 19 = 16%** |

† 1 skipped (sphinx-7757 did not apply, Step 1 REJECT).

Notes on counting: `sphinx-doc__sphinx-11510` is caught independently by both
Step 4 and Step 5 (different signals: AST SyntaxError vs F404). It counts as
one catch toward the pipeline total. `django__django-11790` is caught only by
Step 5 (F811 — valid syntax but redefined name). `sphinx-doc__sphinx-7757` is
caught only by Step 1.

**All non-PASS verdicts are on bad (unresolved) patches.**
**No resolved (correct) patch is rejected or hard-flagged by any check.**

**Unique catches (each caught by exactly one checker):**

| Instance | Ground truth | Caught by | Verdict | Reason |
|---|---|---|---|---|
| `sphinx-doc__sphinx-7757` | unresolved | `check_patch_applies` | REJECT | Patch targets `.orig` backup file — git cannot find the path |
| `sphinx-doc__sphinx-11510` | unresolved | `check_files_parse` + `check_lint_delta` | REJECT | `from docutils import …` before `from __future__` → SyntaxError (AST) + F404 (flake8) |
| `django__django-11790` | unresolved | `check_lint_delta` | FLAG | F811: `__init__` redefined without removing the original definition |

---

## Repository Layout

```
sieve/
├── phases/
│   └── static.py          # All checkers + orchestrator (fully implemented)
├── reproduction/
│   ├── agent.py           # Reproduction agent (flaky — see note below)
│   ├── runner.py          # Batch runner
│   └── prompts.py         # LLM prompt templates
└── utils/
    └── swebench.py        # Dataset loading + Docker env helpers

tests/
└── test_static_checks.py  # 3 unit tests for run_static_checks()

scripts/
├── validate_semgrep.py        # Batch semgrep validation via Docker
└── validate_static_checks.py  # Batch per-checker validation via Docker

runs/
├── static_checks_validation/
│   ├── results.json       # Per-instance verdicts for all 3 checkers
│   └── summary.json       # Aggregated confusion matrices
└── semgrep_validation/
    └── summary.json       # Semgrep post-only mode results (old baseline)
```

---

## Reproduction Agent (`sieve/reproduction/`) — OPTIONAL

> **Status: implemented but flaky. Do not rely on these results until the
> fail-to-pass (F→P) rate is measured on the pilot set.**

Generates a standalone Python script that reproduces the reported bug, then
validates the script fails against the unpatched codebase (F→P check).

**Why it is flaky:**
1. Scripts are generated without seeing the repo, so import paths and API
   shapes are often wrong — the script crashes at setup rather than at the bug.
2. Django instances require `django.setup()` with a minimal settings module,
   which is hard to construct without repo context.
3. The `_is_meaningful_failure()` heuristic can miss subtle cases.

---

## Not Yet Implemented

| Component | Phase | Notes |
|---|---|---|
| Layer 2: dynamic checks | 2 | Reproduction test execution + existing test-suite regression |
| Layer 3: LLM audit | 3 | Rubric-based evaluation via Gemini Flash |
| Feedback aggregator + retry loop | — | Structured JSON feedback → patch agent |
| Phase 3 rule evolution | — | Generate v1 Semgrep rules from failure analysis on the 19 unresolved patches |

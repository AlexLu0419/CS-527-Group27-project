# SIEVE — Implementation Status

> Last updated: 2026-04-15

---

## Repository layout

```
sieve/
├── phases/
│   └── static.py          # Layer 1 static checks (DONE)
├── reproduction/
│   ├── agent.py           # Reproduction agent (OPTIONAL — see note)
│   ├── runner.py          # Batch runner for reproduction agent
│   └── prompts.py         # LLM prompt templates
└── utils/
    └── swebench.py        # Dataset loading + Docker env helpers
```

---

## Layer 1 — Static Checks (`sieve/phases/static.py`)

All of Phase 1.1 and Phase 1.2 are implemented and tested against the 50-instance
Gemini 2.5 Pro pilot set.

### Public API

```python
from sieve.phases.static import check_patch_applies, check_files_parse, check_lint_delta, CheckResult
```

Every function returns a `CheckResult(verdict, check_name, message)` where
`verdict` is one of `"PASS"`, `"REJECT"`, or `"FLAG"`.

---

### 1.1a — `check_patch_applies(repo_path, patch_file)`

Runs `git apply --check <patch_file>` inside `repo_path` (dry-run, no working
tree changes). Returns REJECT with git's stderr on non-zero exit.

**Pilot results (50 instances):**

| Outcome | Count |
|---------|-------|
| REJECT — corrupt patch (truncated LLM output) | 4 |
| REJECT — patch targets wrong file/repo | 2 |
| PASS | 44 |

All 5 instances the SWE-bench harness marked as `error` were caught here.
Zero false positives (no RESOLVED instance was rejected).

---

### 1.1b — `check_files_parse(repo_path, changed_files)`

For each `.py` file touched by the patch:
1. Primary: `ast.parse()` — precise `file:lineno: message` errors.
2. Secondary: `python -m py_compile` (via `sys.executable`) — catches encoding
   and codec issues that `ast.parse` occasionally misses.

Non-Python files and deleted files are silently skipped.

**Pilot results (44 instances passing 1.1a):**

| Outcome | Count |
|---------|-------|
| REJECT — syntax error introduced by patch | 1 |
| PASS | 43 |

Caught instance: `sphinx-doc__sphinx-11510` — LLM inserted two `from docutils import`
lines *above* `from __future__ import annotations`, which is a `SyntaxError` in Python.

---

### 1.2 — `check_lint_delta(repo_path, changed_files, patch_file)`

Runs `flake8 --select=F,E9 --format=json` before and after applying the patch,
then reports only *new* diagnostics introduced by the patch.

**Verdict policy:**

| Code range | Classification | Examples |
|---|---|---|
| `E9*` | REJECT | Runtime syntax errors |
| `F404`, `F821`–`F831`, `F9*`, format codes | REJECT | Undefined names, `from __future__` after other imports |
| `F401`, `F811`, `F841`, `F842`, `F631`, etc. | FLAG | Unused imports, redefined names |
| `E501`, `F402`, `F403`, `F406`, `F407` | ignored | Line length, star imports (too noisy) |

Requires `flake8` and `flake8-json` (`pip install flake8 flake8-json`).

**Known issue — line-number shifting:** When the patch inserts or removes lines,
pre-existing lint issues at shifted positions appear as "new". This produces
occasional false flags on files with many pre-existing warnings. Mitigation:
the pilot corpus is clean enough that this did not fire on any RESOLVED instance.

**Pilot results (44 instances passing 1.1):**

| Outcome | Count |
|---------|-------|
| REJECT — `F404` (`from __future__` ordering) | 1 |
| FLAG — `F811` (redefined name) | 1 |
| PASS | 42 |

Zero false positives.

---

### Combined Layer 1 summary (50 instances)

| Check | New REJECTs | New FLAGs | False positives |
|-------|-------------|-----------|-----------------|
| `check_patch_applies` | 6 | — | 0 |
| `check_files_parse` | 1 | — | 0 |
| `check_lint_delta` | 1 (overlaps with above) | 1 | 0 |
| **Layer 1 total** | **7 unique** | **1** | **0** |

---

## Reproduction Agent (`sieve/reproduction/`) — OPTIONAL ⚠️

> **Status: implemented but flaky. Do not rely on it for evaluation results
> until F→P validation rate is measured and stabilised.**

The reproduction agent generates a standalone Python test that reproduces the
bug described in an issue, then validates that the script *fails* on unpatched
code (F→P check).

### Components

**`agent.py` — `ReproductionAgent`**

- Calls an LLM (default: `gemini/gemini-3-flash-preview` via litellm) with the
  issue title and problem statement.
- Executes the generated script inside the SWE-bench Docker container
  (`/tmp/repro_test.py` on `/testbed`).
- Validates that the exit code is non-zero **and** that the failure is
  *meaningful* (assertion error from the script, not a broken import or Django
  setup crash).
- Refines up to `max_attempts` times (default: 3) using execution feedback.

**`runner.py` — `run_batch()`**

- Wraps `ReproductionAgent` for parallel/sequential batch runs over a list of
  SWE-bench instances.
- Supports resume (skips already-completed instances).
- Saves results incrementally as `{instance_id: ReproductionResult}` JSON.

**`prompts.py`**

- `SYSTEM_PROMPT` — instructs the LLM to write a minimal self-contained script.
- `GENERATE_USER_TEMPLATE` — first-attempt prompt (issue description only, no
  repo context).
- `REFINE_USER_TEMPLATE` — retry prompt with execution output and diagnosis.
- `get_failure_diagnosis()` — classifies the exit code + output into a
  human-readable category to guide refinement.

### Why it is flaky

1. **Low F→P rate without repo context.** The agent deliberately generates
   tests without seeing the codebase (following CodeMonkeys), which means it
   often gets import paths or API shapes wrong and the script crashes at setup
   rather than at the actual bug.
2. **Django setup complexity.** Many Django instances require `django.setup()`
   with a minimal settings module. Getting this right without seeing the repo is
   hit-or-miss.
3. **Validation heuristic fragility.** `_is_meaningful_failure()` uses a
   two-tier pattern-match + traceback-frame analysis. It correctly rejects infra
   crashes but can miss subtle cases.

### Planned stabilisation work

- Measure actual F→P rate on the 50-instance pilot.
- Add a settings-stub injector for Django instances.
- Consider providing the repo file tree (but not source) to the first-attempt
  prompt to improve import accuracy.

---

---

## Layer 1 — Phase 1.3 Semgrep Check (`sieve/phases/static.py`)

### `check_semgrep(repo_path, changed_files, config="p/python")`

Runs `semgrep --config p/python --json --quiet` on the modified `.py` files
as they exist post-patch. All findings are reported as **FLAG** — semgrep
results never REJECT a patch on their own.

No custom YAML rules. Custom rules are deferred to the Phase 3 self-evolution
loop (no `p/sphinx` ruleset exists; `p/python` covers both Django and Sphinx
instances uniformly).

On semgrep execution failure (missing binary, bad config) the function returns
PASS with a warning log so the pipeline is never blocked.

**Pilot results:** Not yet run against the 50-instance set. Smoke-tested locally
— `subprocess.run(cmd, shell=True)` correctly triggers FLAG via
`python.lang.security.audit.subprocess-shell-true`.

---

## Not yet implemented

| Component | Phase | Notes |
|-----------|-------|-------|
| `run_static_checks()` orchestrator | 1.4 | Wraps all Layer 1 checks in order |
| `StaticCheckResult` dataclass | 1.4 | Structured aggregate result |
| Layer 2: dynamic checks | 2 | Reproduction test execution + regression suite |
| Layer 3: LLM audit | 3 | Rubric-based evaluation via Gemini Flash |
| Feedback aggregator + retry loop | — | Structured JSON feedback → patch agent |
| Phase 2 baseline measurement | — | Run all checks on 50-instance pilot |
| Phase 3 rule evolution | — | Generate v1 Semgrep rules from failure analysis |

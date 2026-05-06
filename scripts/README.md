# scripts/

Per-script reference. For pipeline order and end-to-end runbook, see the
[root README](../README.md).

---

## Cascade validators

### `validate_static_checks.py`
Runs Layer 1 (`git apply --check` dry-run) on every patch via Docker exec.
Writes `results.json` + `summary.json` (confusion matrix).

```
--preds <preds.json>      first-run preds (default: gemini pilot)
--gt <gt.json>            SWE-bench ground-truth report
--output-dir <dir>        default: runs/static_checks_validation/
```

### `validate_dynamic_regression.py`
Runs Layer 2a — applies the patch and runs the instance's `PASS_TO_PASS`
tests post-patch only. Any failure → REJECT.

```
--preds <preds.json>
--gt <gt.json>
--output-dir <dir>        default: runs/dynamic_regression_validation/
--timeout <sec>           per-instance pytest timeout (default: 120)
--resume                  skip rows already in results.json
```

### `validate_reproduction.py`
Runs Layer 2b — Phase A (generate + gate reproduction tests) + Phase B
(weighted vote on the patched container).

```
--preds <preds.json>
--gt <gt.json>
--output-dir <dir>        default: runs/reproduction_validation/
--phase-a-cache-dir <dir> default: runs/phase_a_cache
--use-frozen-cache        load Phase A from --phase-a-cache-dir without
                          regenerating on stale fingerprint (A/B mode)
--timeout <sec>           per-test timeout (default: 60)
--only <ids>              comma-separated instance_ids
--resume
```

### `validate_judge.py`
Runs Layer 3 only on instances whose reproduction verdict is `UNCERTAIN` or
`UNCERTAIN_ZERO_SIGNAL`. Requires `validate_reproduction.py` output.

```
--preds <preds.json>
--repro <results.json>    reproduction results (default: gemini pilot path)
--output-dir <dir>        default: runs/judge_validation/
--only <ids>
--resume
```

---

## Retry pipeline

### `build_retry_manifest.py`
Reads the four cascade `results.json` files, selects every instance whose
final verdict isn't PASS (i.e. FAIL / ERROR / MISSING / UNCERTAIN), and writes
a per-instance feedback payload + a regex filter.

```
--static <results.json>
--dynreg <results.json>
--repro <results.json>
--judge <results.json>
--preds <preds.json>            first-run patches
--phase-a-cache <dir>           default: runs/phase_a_cache
--focal-snippets-dir <dir>      default: runs/focal_snippets_cache
--output-dir <dir>              default: runs/retry_gpt5mini
--no-judge-block                omit judge-rubric block
```

Outputs `feedback_manifest.json` + `retry_filter.txt`.

### `extract_focal_snippets.py`
Spins up Docker per instance, extracts ~30 lines around the focal symbol
identified in Phase A, and caches to `runs/focal_snippets_cache/{iid}.json`.
Run before `build_retry_manifest.py` so the retry feedback can include focal
source without needing Docker.

```
--preds <preds.json>
--phase-a <dir>           Phase A cache dir
--out-dir <dir>           default: runs/focal_snippets_cache/
--filter <retry_filter.txt>   restrict to IDs in a retry roster
--only <ids>              comma-separated instance_ids
--force                   re-extract even if cache hit
```

### `merge_retry_preds.py`
Layered merge of first-run + retry preds. The retry layer only overwrites
when its `model_patch` is non-empty.

```
--first-run <preds.json>  required
--retry <preds.json>      required
--out <preds.json>        required
```

---

## Phase A pilot

### `run_phase_a_only.py`
Drives Phase A (generate + gate) for every instance in a preds.json and
prints per-instance bucket counts. Skips Phase B / cascade / retry. Useful
fail-fast check before committing full cascade budget.

```
--preds <preds.json>
--summary-out <path>
--only <ids>              comma-separated instance_ids
```

Phase A artifacts land in `runs/phase_a_cache/`.

---

## Reporting

### `cascade_matrix.py`
Given the four cascade `results.json` files and a ground-truth report,
prints the TP/FP/TN/FN/UNCERTAIN confusion matrix for the final cascade
verdict and lists the FP/FN instance IDs.

```
--static <results.json>   required
--dynreg <results.json>   required
--repro <results.json>    required
--judge <results.json>    required
--gt <gt.json>            required
--label <str>             header above the matrix (default: "cascade")
```
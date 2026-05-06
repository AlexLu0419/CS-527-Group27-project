# Self-Improving through Evaluation and Verification for software Engineering agents (SIEVE)

SIEVE is a cascading verifier for SWE-agent-generated patches. Given an issue and a
candidate patch, it runs a four-layer cascade — **static → regression → reproduction →
judge** — and emits a single final verdict (`PASS`, `FAIL`, or `UNCERTAIN`). FAIL and
UNCERTAIN instances are then re-run with synthesized feedback (the **retry pass**),
re-merged with the first-run preds, and evaluated against SWE-bench ground truth.

The 50-instance SWE-bench Verified subset under evaluation is enumerated in
`data/instance_ids.json`. The vendored `mini-swe-agent/` is the external patch author
that produces preds.json files; SIEVE itself never writes patches.

---

## Setup

### Prerequisites

- Python 3.12+
- Docker (required for regression + reproduction layers and for running mini-swe-agent)
- [uv](https://docs.astral.sh/uv/) for dependency management

### Install

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

### API keys

```bash
export OPENAI_API_KEY=...
export GEMINI_API_KEY=...
```

`configs/models.json` declares which env var each provider expects; the secret stays in
the environment.

---

## Model configuration (`configs/models.json`)

Single source of truth for every LLM role. The current mapping:

| Role        | Model                       | Why |
|-------------|-----------------------------|-----|
| `localize`  | `gemini/gemini-2.5-pro`     | Phase A localization. |
| `generate`  | `openai/gpt-5-mini`         | Phase A reproduction-test generation. |
| `feedback`  | `gemini/gemini-2.5-flash`   | Per-bucket failure-mode synthesis (FAIL paths). |
| `judge`     | `gemini/gemini-2.5-pro`     | Cross-family judge for UNCERTAIN cases. |
| `mini_swe_agent` | `openai/gpt-5-mini`    | First-run + retry patch author. |

Override any role at runtime with `SIEVE_MODEL_<ROLE>=...` (see
`sieve/llm/roles.py`). 

---

## The cascade

```
patch ──▶  L1 static ──REJECT──▶ FAIL
            │PASS
            ▼
          L2a regression ──REJECT──▶ FAIL
            │PASS
            ▼
          L2b reproduction
            ├─ PASS ─────────────────▶ PASS (final)
            ├─ FAIL ─────────────────▶ FAIL (final)
            ├─ UNCERTAIN_ZERO_SIGNAL ─▶ UNCERTAIN (final)
            └─ UNCERTAIN
                  │
                  ▼
                L3 judge ──aggregate──▶ PASS | FAIL | UNCERTAIN
```

- **L1 static** ([sieve/phases/static.py](sieve/phases/static.py)) — single check:
  `git apply --check`.
- **L2 regression** ([sieve/phases/dynamic.py](sieve/phases/dynamic.py)) — applies
  the patch and runs the instance's `PASS_TO_PASS` tests once on the patched repo.
  Any failure is a hard REJECT.
- **L3 reproduction** ([sieve/phases/reproduction.py](sieve/phases/reproduction.py),
  [sieve/repro/](sieve/repro/)) — Phase A (patch-blind) uses `localize` → `generate`
  to produce reproduction-test candidates per mask. Phase B runs each gated
  candidate against the patched container and computes a weighted vote across A/B/C
  buckets.
- **L4 judge** ([sieve/phases/judge.py](sieve/phases/judge.py),
  [sieve/judge/](sieve/judge/)) — fires only on UNCERTAIN from L3. A 4-item rubric
  prompt produces per-criterion scores; `aggregate()` combines them with the repro
  signal into the final verdict.

---

## End-to-end runbook

The pipeline is a sequence of independent scripts that read each other's JSON
outputs. Run them in this order. Defaults below assume the canonical
gpt-5-mini run; pass `--preds` / `--gt` / `--output-dir` to retarget.

### 1. Generate first-run patches

We use [mini-swe-agent](mini-swe-agent/) as the model harness — it is the
external SWE-agent that authors patches from issue text. SIEVE itself never
writes patches; it only verifies them. 

```bash
FILTER=$(python3 -c "import json; print('|'.join(json.load(open('data/instance_ids.json'))['instance_ids']))")

cd mini-swe-agent
python -m minisweagent.run.benchmarks.swebench \
    --subset verified --split test \
    --filter "^($FILTER)$" \
    --output ../runs/swe-verified_50_gpt5-mini \
    -m openai/gpt-5-mini \
    -w 2
cd ..
```

Output: `runs/swe-verified_50_gpt5-mini/preds.json`.

### 2. Get ground-truth labels

Local:

```bash
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --predictions_path runs/swe-verified_50_gpt5-mini/preds.json \
    --max_workers 2 \
    --run_id gpt5-mini-firstrun
```

Drop the resulting report under `runs/sb-cli-reports/` (or wherever you point
`--gt` at later).

### 3. Layer 1 — static (`git apply --check`)

```bash
uv run python scripts/validate_static_checks.py \
    --preds runs/swe-verified_50_gpt5-mini/preds.json \
    --gt runs/sb-cli-reports/<your-firstrun-report>.json \
    --output-dir runs/static_checks_validation_gpt5mini
```

Output: `results.json`, `summary.json`. One sub-check per row:
`check_patch_applies` ∈ {PASS, REJECT, ERROR}.

### 4. Layer 2a — regression (PASS_TO_PASS, post-patch only)

```bash
uv run python scripts/validate_dynamic_regression.py \
    --preds runs/swe-verified_50_gpt5-mini/preds.json \
    --gt runs/sb-cli-reports/<your-firstrun-report>.json \
    --output-dir runs/dynamic_regression_validation_gpt5mini \
    --resume
```

`--resume` skips already-completed rows.

### 5. (Optional) Phase A pilot

If you want to inspect Phase A bucket counts before committing full Phase B
budget:

```bash
uv run python scripts/run_phase_a_only.py \
    --preds runs/swe-verified_50_gpt5-mini/preds.json
```

Phase A artifacts land in `runs/phase_a_cache/<fingerprint>.json`; Phase B
(step 6) reads the same cache, so this step is purely informational.

### 6. Layer 2b — reproduction (Phase A + Phase B)

```bash
uv run python scripts/validate_reproduction.py \
    --preds runs/swe-verified_50_gpt5-mini/preds.json \
    --gt runs/sb-cli-reports/<your-firstrun-report>.json \
    --output-dir runs/reproduction_validation_gpt5mini \
    --resume
```

Output verdict per row: PASS | FAIL | UNCERTAIN | UNCERTAIN_ZERO_SIGNAL | ERROR.

### 7. Layer 3 — judge (UNCERTAIN bucket only)

```bash
uv run python scripts/validate_judge.py \
    --preds runs/swe-verified_50_gpt5-mini/preds.json \
    --repro runs/reproduction_validation_gpt5mini/results.json \
    --output-dir runs/judge_validation_gpt5mini \
    --resume
```

The judge is invoked only for instances whose reproduction verdict is UNCERTAIN
or UNCERTAIN_ZERO_SIGNAL.

### 8. Cascade confusion matrix

```bash
uv run python scripts/cascade_matrix.py \
    --static  runs/static_checks_validation_gpt5mini/results.json \
    --dynreg  runs/dynamic_regression_validation_gpt5mini/results.json \
    --repro   runs/reproduction_validation_gpt5mini/results.json \
    --judge   runs/judge_validation_gpt5mini/results.json \
    --gt      runs/sb-cli-reports/<your-firstrun-report>.json \
    --label   "first-run cascade"
```

Prints TP/FP/TN/FN/UNCERTAIN counts and lists the FP/FN instances.

### 9. Pre-cache focal snippets (used by retry feedback)

```bash
uv run python scripts/extract_focal_snippets.py
```

Caches Docker-extracted focal-file snippets to `runs/focal_snippets_cache/{iid}.json`.
The retry-manifest builder reads these without needing Docker.

### 10. Build the retry feedback manifest

The manifest covers every instance whose final cascade verdict isn't PASS
(see *Retry inclusion semantics* above). Empty patches and harness-error IDs
are included automatically.

```bash
uv run python scripts/build_retry_manifest.py \
    --static  runs/static_checks_validation_gpt5mini/results.json \
    --dynreg  runs/dynamic_regression_validation_gpt5mini/results.json \
    --repro   runs/reproduction_validation_gpt5mini/results.json \
    --judge   runs/judge_validation_gpt5mini/results.json \
    --preds   runs/swe-verified_50_gpt5-mini/preds.json \
    --output-dir runs/retry_gpt5mini
```

Outputs:
- `runs/retry_gpt5mini/feedback_manifest.json` — per-instance feedback text
  (prior patch + static block + regression block + reproduction block; **judge
  output is intentionally excluded** from the feedback to avoid over-committing
  the retry agent). For ERROR/MISSING layers, the corresponding block reports
  the error or "MISSING (no record for this instance)".
- `runs/retry_gpt5mini/retry_filter.txt` — anchored regex to feed mini-swe-agent.

### 11. Re-run mini-swe-agent on the retry filter

```bash
cd mini-swe-agent
python -m minisweagent.run.benchmarks.swebench \
    --subset verified --split test \
    --filter "$(cat ../runs/retry_gpt5mini/retry_filter.txt)" \
    --feedback-manifest ../runs/retry_gpt5mini/feedback_manifest.json \
    --output ../runs/retry_gpt5mini_preds \
    -m openai/gpt-5-mini \
    -w 2
cd ..
```

### 12. Merge first-run + retry preds

```bash
uv run python scripts/merge_retry_preds.py \
    --first-run runs/swe-verified_50_gpt5-mini/preds.json \
    --retry     runs/retry_gpt5mini_preds/preds.json \
    --out       runs/merged_preds_gpt5mini.json
```

The retry layer only replaces an entry when its `model_patch` is non-empty —
empty / failed retry preds fall back to the first-run patch.

### 13. Evaluate the merged preds against SWE-bench

```bash
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --predictions_path runs/merged_preds_gpt5mini.json \
    --max_workers 2 \
    --run_id gpt5-mini-merged
```

### 14. Pipeline report (cost + layer pass-through)

```bash
uv run python scripts/pipeline_report.py \
    --since         <unix-timestamp-of-pipeline-start> \
    --retry-dir     runs/retry_gpt5mini \
    --retry-preds-dir runs/retry_gpt5mini_preds \
    --repro-results runs/reproduction_validation_gpt5mini/results.json \
    --harness-report runs/local_eval/<your-merged-report>.json \
    --out           docs/<your-report>.md
```

Aggregates LLM token spend (from `runs/llm_log/*.jsonl`) plus mini-swe-agent
costs (from per-instance `traj.json`) and renders a Markdown report with the
cost table, per-layer verdict counts, and final cascade-vs-harness numbers.

---

## Per-layer validator reference

All validators are independent and idempotent. Each writes
`results.json` + `summary.json` under its `--output-dir`.

| Script | Layer | Output dir (default) |
|---|---|---|
| [scripts/validate_static_checks.py](scripts/validate_static_checks.py) | L1 static (git apply --check) | `runs/static_checks_validation/` |
| [scripts/validate_dynamic_regression.py](scripts/validate_dynamic_regression.py) | L2a regression (PASS_TO_PASS) | `runs/dynamic_regression_validation/` |
| [scripts/validate_reproduction.py](scripts/validate_reproduction.py) | L2b reproduction (Phase A+B); pass `--use-frozen-cache --phase-a-cache-dir <dir>` for A/B against a frozen snapshot | `runs/reproduction_validation/` |
| [scripts/validate_judge.py](scripts/validate_judge.py) | L3 judge (UNCERTAIN bucket) | `runs/judge_validation/` |
| [scripts/run_phase_a_only.py](scripts/run_phase_a_only.py) | Phase A pilot driver | (writes to `runs/phase_a_cache/`) |

All accept `--help`. Most accept `--resume` to skip already-completed rows.

---

## Phase A caching

[sieve/repro/cache.py](sieve/repro/cache.py) fingerprints the mask YAMLs and the
issue text (not the candidate patch) and caches generated reproduction tests
under `runs/phase_a_cache/<fingerprint>.json`. Two different candidate patches
for the same issue reuse the same Phase A artifacts, so swapping the patch
author doesn't invalidate the cache. The cache is safe to delete; it will be
repopulated on the next `validate_reproduction.py` run.

For A/B comparisons against a frozen Phase A snapshot, run
[scripts/validate_reproduction.py](scripts/validate_reproduction.py) with
`--phase-a-cache-dir <archived-dir> --use-frozen-cache` — that loads the
cached entries directly without regenerating Phase A on stale fingerprints.

---

## Repository layout

```
.
├── configs/models.json             # LLM role → model mapping
├── data/instance_ids.json          # 50-instance SWE-bench Verified ID list
├── docs/                           # design notes + per-version pipeline reports
├── mini-swe-agent/                 # vendored external patch-author agent
├── runs/                           # cached artifacts & results (mostly gitignored)
│   ├── phase_a_cache/              # patch-blind reproduction-test cache
│   ├── focal_snippets_cache/       # per-instance focal-file snippets
│   ├── static_checks_validation*/
│   ├── dynamic_regression_validation*/
│   ├── reproduction_validation*/
│   ├── judge_validation*/
│   ├── retry_gpt5mini*/            # feedback manifest + retry preds
│   ├── swe-verified_50_*/          # mini-swe-agent first-run preds
│   └── sb-cli-reports/             # ground-truth labels from SWE-bench harness
├── scripts/                        # entrypoints (validators, retry, reporting)
└── sieve/
    ├── llm/         # litellm client, role resolution, dry-run stub
    ├── phases/      # static, dynamic (regression), reproduction, judge
    ├── repro/       # Phase A/B: localize, generate, skeleton, runner, gate, cache
    ├── judge/       # rubric assembly, aggregation
    ├── prompts/     # Jinja2 YAML prompts + ARTIFACT_VERSIONS.json
    └── utils/       # SWE-bench dataset helpers
```

---

## Helper scripts

| Script | Purpose |
|---|---|
| [scripts/extract_dataset_snapshot.py](scripts/extract_dataset_snapshot.py) | Snapshot the SWE-bench rows (for offline analysis). |
| [scripts/extract_focal_snippets.py](scripts/extract_focal_snippets.py) | Pre-cache focal-file snippets for retry feedback. |
| [scripts/reaggregate_with_new_routing.py](scripts/reaggregate_with_new_routing.py) | Re-run reproduction-layer aggregation with a different routing rule (no Docker, no LLM). |
| [scripts/cascade_matrix.py](scripts/cascade_matrix.py) | Confusion matrix across the four cascade JSONs. |
| [scripts/build_retry_manifest.py](scripts/build_retry_manifest.py) | Feedback manifest + retry filter for FAIL / ERROR / MISSING instances. |
| [scripts/merge_retry_preds.py](scripts/merge_retry_preds.py) | Fallback-on-empty merge of first-run + retry preds into a single preds.json. |
| [scripts/pipeline_report.py](scripts/pipeline_report.py) | End-to-end cost + verdict report. |

---
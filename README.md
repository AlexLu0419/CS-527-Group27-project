# Self-Improving Evaluation via Verification Evolution for SWE-Agents (SIEVE)

SIEVE is a cascading verifier for SWE-agent-generated patches. Given an issue and a
candidate patch, it runs a four-layer cascade — **static → regression → reproduction →
judge** — and emits a single final verdict (`PASS`, `FAIL`, or `UNCERTAIN`). The repo
also ships a **self-evolution loop** that classifies the verifier's own mistakes on a
labeled slice, points at which prompt/rubric/threshold to edit, and validates the edit
before the change is kept.

The historical patch set under evaluation lives in `runs/swe-verified_50_gemini-2.5-pro/`
(50-instance SWE-bench Verified subset). The vendored `mini-swe-agent/` is the external
patch author used to generate new patches; SIEVE itself never writes code.

---

## Repository layout

```
.
├── configs/models.json             # single source of truth for LLM role → model mapping
├── data/                           # 50-instance SWE-bench Verified ID list
├── docs/                           # design notes (v2/v3 plans, refined ideas, prompt pack)
├── mini-swe-agent/                 # vendored external patch-author agent
├── runs/                           # cached artifacts & results (gitignored in practice)
│   ├── evolution/                  # slice_manifest.json, per-cycle cascade runs
│   ├── phase_a_cache/              # patch-blind reproduction-test cache (auto-created)
│   ├── static_checks_validation/
│   ├── dynamic_regression_validation/
│   ├── swe-verified_50_gemini-2.5-pro/   # preds.json under evaluation
│   └── sb-cli-reports/             # ground-truth labels
├── scripts/                        # entrypoints: validators, runner, evolution tools
├── sieve/
│   ├── llm/          # litellm client, role resolution, cache, dry-run stub
│   ├── phases/       # static, dynamic (regression), reproduction, judge
│   ├── repro/        # Phase A/B: localize, generate, skeleton, runner, feedback, gate
│   ├── judge/        # rubric assembly, strip, aggregation
│   ├── evolution/    # versions, classify, report
│   ├── prompts/      # Jinja2 YAML prompts + ARTIFACT_VERSIONS.json
│   └── utils/        # SWE-bench dataset helpers
└── tests/
```

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

SIEVE talks to LLM providers via `litellm`. Only OpenAI and Gemini are used:

```bash
export OPENAI_API_KEY=...
export GEMINI_API_KEY=...
```

`configs/models.json` declares *which* env var each provider expects; the secret itself
stays in the environment.

---

## Model configuration (`configs/models.json`)

Single source of truth for every LLM role in the cascade and for the mini-swe-agent
patch author. The file is checked in — no secrets, only identifiers.

```json
{
  "sieve_roles": {
    "localize": "gemini/gemini-2.5-flash",
    "generate": "openai/gpt-5-mini",
    "feedback": "gemini/gemini-2.5-flash",
    "judge":    "gemini/gemini-2.5-pro"
  },
  "mini_swe_agent": "openai/gpt-5-mini",
  "providers": {
    "openai": {"env_var": "OPENAI_API_KEY"},
    "gemini": {"env_var": "GEMINI_API_KEY"}
  },
  "env_overrides": {
    "localize": "SIEVE_MODEL_LOCALIZE",
    "generate": "SIEVE_MODEL_GENERATE",
    "feedback": "SIEVE_MODEL_FEEDBACK",
    "judge":    "SIEVE_MODEL_JUDGE"
  }
}
```

**Resolution precedence** (see `sieve/llm/roles.py`):

1. Explicit argument to `resolve_model(role, override=...)`
2. Per-role env var (`SIEVE_MODEL_LOCALIZE`, etc.)
3. JSON entry under `sieve_roles`
4. Hardcoded fallback in `roles.py` (same defaults as the JSON)

**Role rationale**

| Role | Model | Why |
|---|---|---|
| `localize` | `gemini/gemini-2.5-flash` | Cheap, JSON-mode reliable, called 3× per instance. |
| `generate` | `openai/gpt-5-mini` | Must not exceed the weakest agent under test; Phase A is patch-blind, so same-family risk is low. |
| `feedback` | `gemini/gemini-2.5-flash` | Nuanced failure-mode articulation on FAIL paths only. |
| `judge`    | `gemini/gemini-2.5-pro`  | Highest-stakes decision; cross-family against GPT-5-mini patch author minimizes self-eval bias. |

---

## The SIEVE cascade

`sieve/phases/` implements four layers, chained by `scripts/run_sieve_v0.py`:

```
             ┌────────────────────────────────────────────────┐
patch ──▶  L1 static  ──REJECT──▶ FAIL                         │
             │PASS/FLAG                                         │
             ▼                                                  │
           L2a regression ──REJECT──▶ FAIL                      │
             │PASS                                              │
             ▼                                                  │
           L2b reproduction                                     │
             ├─ PASS / FAIL ─────────────────────▶ final        │
             ├─ UNCERTAIN_ZERO_SIGNAL ─────────▶ UNCERTAIN      │
             └─ UNCERTAIN                                       │
                │                                               │
                ▼                                               │
              L3 judge ──aggregate──▶ PASS | FAIL | UNCERTAIN ◀┘
```

**L1 static** (`sieve/phases/static.py`) — `check_patch_applies`, `check_files_parse`,
`check_lint_delta` (flake8 + semgrep). Deterministic, no LLM.

**L2a regression** (`sieve/phases/dynamic.py`) — runs the instance's `PASS_TO_PASS` tests
in the SWE-bench Docker harness; any regression is a hard REJECT.

**L2b reproduction** (`sieve/phases/reproduction.py`, `sieve/repro/`) — Phase A (patch-
blind) uses `localize` → `generate` to produce 2 reproduction test candidates per mask
skeleton (raw / traceback-first / snippet-first). Phase B runs each candidate pre- and
post-patch inside Docker; on per-bucket FAIL, `feedback` synthesizes a failure-mode
triple that feeds the next generation attempt (`sieve/repro/gate.py`).

**L3 judge** (`sieve/phases/judge.py`, `sieve/judge/`) — fires only on `UNCERTAIN` from
L2b. A rubric prompt (`sieve/prompts/judge_patch.yaml`) produces per-criterion scores;
`aggregate()` combines them with the repro score into the final verdict.

**Artifact versioning** — every prompt YAML has a `sha256` content hash in
`sieve/prompts/ARTIFACT_VERSIONS.json`. Each per-instance result records the
`layer_snapshots` dict, so we can always tell which prompt version produced which
verdict. Edit a prompt → version bumps → snapshot diverges → evolution delta is
attributable.

---

## Running the cascade

### Single instance, dry-run

No Docker or LLM calls; uses `sieve/llm/dry_run.py` stubs. Useful for config / import
sanity checks.

```bash
SIEVE_DRY_RUN=1 uv run python scripts/run_sieve_v0.py \
    --manifest runs/evolution/slice_manifest.json \
    --split evolution \
    --only django__django-11815 \
    --out /tmp/smoke.json
```

### Full offline cascade on the evolution slice

Needs Docker running + real API keys.

```bash
uv run python scripts/run_sieve_v0.py \
    --manifest runs/evolution/slice_manifest.json \
    --split evolution \
    --out runs/evolution/sieve_v0_evolution_results.json \
    --reuse-static --reuse-regression
```

Flags:

| Flag | Meaning |
|---|---|
| `--manifest` | Slice manifest from `scripts/build_evolution_slice.py`; omit to run all labeled instances. |
| `--split` | `evolution` (35), `held_out` (10), or `all`. |
| `--reuse-static` / `--reuse-regression` | Load cached verdicts from `runs/*_validation/results.json` instead of recomputing. |
| `--only` | Comma-separated instance IDs to whitelist. |
| `--resume` | Skip instance IDs already present in `--out`. |

The output JSON is a list of per-instance records: `layers.{static,regression,reproduction,judge}`
sub-verdicts, `final_verdict`, `final_source`, and the `layer_snapshots` fingerprint.

---

## Per-layer validation scripts

Each validator runs one layer in isolation against the 50-instance benchmark and drops a
results JSON under `runs/<name>_validation/`. These are how you re-materialize the
caches that `run_sieve_v0.py` consumes.

| Script | What it validates |
|---|---|
| `scripts/validate_static_checks.py`   | L1 static on all candidate patches. |
| `scripts/validate_semgrep.py`         | Semgrep rules independently of the static layer. |
| `scripts/validate_dynamic_regression.py` | L2a regression-only. |
| `scripts/validate_dynamic_checks.py`  | L2a + L2b dynamic checks together. |
| `scripts/validate_reproduction.py`    | L2b reproduction (Phase A+B) end-to-end. |
| `scripts/validate_judge.py`           | L3 judge rubric on a pre-selected UNCERTAIN set. |

All validators accept `--help`. Typical invocation:

```bash
uv run python scripts/validate_static_checks.py \
    --preds runs/swe-verified_50_gemini-2.5-pro/swe_verified_50_gemini-2.5-pro-new/preds.json \
    --out runs/static_checks_validation/results.json
```

---

## Self-evolution workflow

Each cycle walks the verifier through: **run → classify its own errors → generate a
repair report → (human) edit the prompt → validate the delta**.

### 1. Build the slice manifest (one-time, seeded, reproducible)

```bash
uv run python scripts/build_evolution_slice.py --seed 42 --holdout 10
# → runs/evolution/slice_manifest.json  (35 evolution + 10 held-out, stratified)
```

### 2. Run the cascade on the evolution split

See *Full offline cascade* above; output lives at
`runs/evolution/sieve_v0_evolution_results.json`.

### 3. Classify errors and generate a repair report

`sieve/evolution/classify.py` implements the v3 §5.2 rule table: each mis-verdict is
bucketed as `false_pass` / `false_fail` / `uncertain_leakage` with an
`artifact_edit_hint` pointing at a specific prompt/rubric/threshold.
`sieve/evolution/report.py` groups buckets by hint and writes a Markdown report.

```bash
uv run python -c "
from pathlib import Path
import json
from sieve.evolution.classify import classify_errors
from sieve.evolution.report import build_report

records = json.loads(Path('runs/evolution/sieve_v0_evolution_results.json').read_text())
buckets = classify_errors(records)
manifest = json.loads(Path('runs/evolution/slice_manifest.json').read_text())
build_report(buckets, manifest, Path('runs/evolution/report_cycle1.md'), cycle=1)
"
```

### 4. Edit the pointed-at artifact

Open the YAML named in the report's `artifact_edit_hint` (e.g. `mask_traceback_first`,
`judge_patch`, `feedback_synth`). Make one targeted change. The content hash in
`ARTIFACT_VERSIONS.json` must be re-computed — the harness does this on load, so the
next cascade run records the new snapshot automatically.

### 5. Validate the delta before keeping the edit

```bash
uv run python scripts/validate_evolution_delta.py \
    --baseline-sha <pre-edit commit> \
    --candidate-sha HEAD \
    --manifest runs/evolution/slice_manifest.json
```

The script spawns a detached git worktree at the baseline SHA, reruns `run_sieve_v0.py`
on *both* splits at both refs, and applies the acceptance rule:

> `held_out_accuracy(v1) > v0  AND  evolution_accuracy(v1) ≥ v0 − 0.02`

If the candidate fails, the script prints (but does **not** auto-run) a revert command.

---

## Running mini-swe-agent (generate new patches)

The vendored agent in `mini-swe-agent/` is used to produce patches under `runs/`, which
SIEVE then evaluates. Its model comes from `configs/models.json::mini_swe_agent`, which
is wired into `mini-swe-agent/src/minisweagent/config/benchmarks/swebench.yaml` (and
`swebench_backticks.yaml`).

### Install

```bash
cd mini-swe-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cd ..
```

### Run on the 50-instance subset

```bash
FILTER=$(python3 -c "
import json
ids = json.load(open('data/instance_ids.json'))['instance_ids']
print('|'.join(ids))
")

python -m minisweagent.run.benchmarks.swebench \
    --subset verified \
    --split test \
    --filter "^($FILTER)$" \
    --output runs/swe-verified_50_gpt5-mini \
    -m openai/gpt-5-mini \
    -w 2
```

| Flag | Description |
|---|---|
| `--subset verified` | `princeton-nlp/SWE-Bench_Verified` from HuggingFace. |
| `--filter` | Regex to select the 50 instances in `data/instance_ids.json`. |
| `--output` | Destination for trajectories + `preds.json`. |
| `-m` | Model name (litellm); default matches `configs/models.json`. |
| `-w` | Parallel Docker workers. `2` is the sweet spot for cost/rate limits. |

### Resuming a partial run

```bash
python3 -c "
import json, yaml
all_ids = set(json.load(open('data/instance_ids.json'))['instance_ids'])
statuses = yaml.safe_load(open('runs/swe-verified_50_gpt5-mini/exit_statuses_*.yaml'))
submitted = set(statuses['instances_by_exit_status'].get('Submitted', []))
print('|'.join(sorted(all_ids - submitted)))
"
```

Rerun with that filter and the same `--output`; already-submitted instances are skipped
via `preds.json`.

### Evaluating the resulting patches against SWE-bench ground truth

Local:

```bash
pip install swebench
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Verified \
    --predictions_path runs/swe-verified_50_gpt5-mini/preds.json \
    --max_workers 4 \
    --run_id gpt5-mini-run
```

Cloud (no Docker):

```bash
pip install sb-cli
sb-cli submit swe-bench_verified test \
    --predictions_path runs/swe-verified_50_gpt5-mini/preds.json \
    --run_id gpt5-mini-run
```

Report JSON lands in `runs/sb-cli-reports/`; the 50-instance ground-truth labels live
there and are what SIEVE compares against.

---

## Phase A caching

`sieve/repro/cache.py::phase_a()` fingerprints the mask YAMLs and the issue text (not
the candidate patch) and caches the generated reproduction tests under
`runs/phase_a_cache/<fingerprint>.json`. Two different candidate patches for the same
issue reuse the same Phase A artifacts, so swapping the patch author doesn't invalidate
the cache. The cache is safe to delete; it will be repopulated on the next run.

---

## Dry-run mode

Set `SIEVE_DRY_RUN=1` to replace every `litellm.completion` call with a deterministic
stub from `sieve/llm/dry_run.py`. Useful for:

- Confirming imports and config wiring after a refactor.
- CI-style smoke tests without burning API credits.
- Reproducing the cascade's control flow on a laptop with no Docker (most layers will
  still error without Docker, but imports and LLM paths are exercised).

---

## Key design documents

| File | Contents |
|---|---|
| `docs/SIEVE_v3_project_plan.md`   | Current cascade + evolution design. |
| `docs/SIEVE_v3_prompt_pack.md`    | Prompt-by-prompt spec for every role. |
| `docs/SIEVE_refined_ideas.md`     | Background reasoning, rejected alternatives. |
| `docs/implementation_status.md`   | Task-level progress against the plan. |

---

## Troubleshooting

- **`ANTHROPIC_API_KEY` error.** Nothing in the repo should reference Anthropic anymore.
  Grep for stray references: `grep -rn "anthropic\|claude-" sieve/ scripts/ configs/`.
- **Docker-in-Docker permission errors during regression/repro.** Make sure the host
  Docker daemon is running (`docker info`) and the user has group access.
- **litellm `UnsupportedParamError` on Gemini.** `drop_params: true` is set in the
  mini-swe-agent YAMLs; for SIEVE's own client, see `sieve/llm/client.py::_call_llm`.
- **Phase A cache corruption.** Delete `runs/phase_a_cache/` — it will be regenerated.
- **Evolution delta rejected.** Read the report at `runs/evolution/report_cycleN.md` —
  the `artifact_edit_hint` tells you which prompt to edit and the evidence shows
  representative instance IDs. Do one change at a time.

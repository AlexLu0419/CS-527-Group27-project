# SIEVE: Self-Improving Evaluation via Verification Evolution for SWE Agents

A multi-method evaluation cascade that sits above a mini-SWE-agent to catch failed patches *before* ground-truth test execution, provide structured diagnostic feedback for retry, and evolve its evaluation rules from observed failures.

**Motivation.** Claude's C Compiler (186k lines of Rust, 16 parallel agents) compiled 2,844 Linux kernel files but couldn't compile "Hello World" — hardcoded GCC include paths only went up to GCC 14. The agents optimized for their test target, not the real world. SIEVE asks: *can we build an evaluation layer that catches these failures before deployment, and can that layer learn to get better over time?*

## Overview

Current SWE-bench evaluation relies entirely on hidden ground-truth test suites. But in real-world software engineering, comprehensive tests often don't exist. SIEVE explores a complementary approach: a **cascading verification pipeline** that combines non-LLM checks (static analysis, linting, existing repo tests) with LLM-based evaluation (rubric-guided judgment) to filter patches — and then **evolves its rules** from the failures it observes.

The key idea is a three-phase cascade ordered by cost and determinism:

```
                     Candidate Patch
                           │
                           ▼
              ┌─────────────────────┐
              │   Phase 1: Static   │  Parse check, lint delta,
              │     (Non-LLM)       │  Semgrep rules (evolvable)
              └──────────┬──────────┘
                   PASS   │   REJECT → structured feedback → retry
                         ▼
              ┌─────────────────────┐
              │  Phase 2: Dynamic   │  Existing repo test suite,
              │     (Non-LLM)       │  import/dependency checks
              └──────────┬──────────┘
                   PASS   │   REJECT → structured feedback → retry
                         ▼
              ┌─────────────────────┐
              │   Phase 3: LLM      │  Rubric evaluation (evolvable),
              │  (Gemini Flash)     │  issue reproduction reasoning
              └──────────┬──────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │     Aggregator      │  Verdict + diagnostic feedback
              └──────────┬──────────┘
                    ┌────┴────┐
                    ▼         ▼
               [ACCEPT]   [REJECT]
                Submit    Feedback → Agent retries (up to K times)
```

After a pilot run, SIEVE analyzes its own false positives (correct patches wrongly rejected) and false negatives (bad patches wrongly accepted) to **evolve** its Semgrep rules and LLM evaluation rubrics — then validates that the evolved rules generalize to unseen instances.

## Method

### Phase 1: Static Checks (Non-LLM, Fast, High-Precision)

These run on every patch at near-zero cost.

| Layer | What it checks | Signal |
|-------|---------------|--------|
| **Patch Validity** | Does the diff apply? Does patched code parse? | Hard reject on syntax errors |
| **Lint Delta** | New pylint/flake8 warnings vs. pre-patch baseline | Flag new errors, pass on warnings |
| **Semgrep Rules** | Custom pattern rules (evolvable from failure analysis) | Flag matches for downstream evaluation |

The Semgrep rules start with a generic set (bare excepts, unused imports, hardcoded paths) and are evolved after the pilot run based on observed failure patterns.

### Phase 2: Dynamic Checks (Non-LLM, Moderate Cost)

| Layer | What it checks | Signal |
|-------|---------------|--------|
| **Existing Test Suite** | Run the repo's own tests (NOT hidden ground-truth) on patched code | Hard reject on new test failures |
| **Import Validation** | Do new imports resolve? Circular dependency check | Hard reject on import errors |

Running the repository's pre-existing test suite is the single most powerful non-LLM check — a patch that breaks existing tests is almost certainly wrong.

### Phase 3: LLM-Based Evaluation (Higher Cost, Nuanced)

Only patches surviving Phase 1–2 reach here.

| Layer | What it checks | Signal |
|-------|---------------|--------|
| **Rubric Evaluation** | Multi-criteria assessment: issue alignment, correctness, scope, regression risk, completeness | Structured per-criterion scores |
| **Reproduction Reasoning** | Traces through the bug scenario with the patch applied | LIKELY_FIXED / UNCERTAIN / NOT_FIXED |

The evaluator LLM (Gemini Flash) is deliberately different from the generator LLM (GPT-5-mini) to avoid shared blind spots.

### Feedback & Retry

When SIEVE rejects a patch, it produces structured diagnostic feedback:

```json
{
  "verdict": "REJECT",
  "confidence": 0.85,
  "reasons": [
    "Phase 1: New flake8 error E302 in models.py:47",
    "Phase 3: Rubric 'COMPLETENESS' failed — patch handles main case but misses None input edge case mentioned in issue paragraph 2"
  ],
  "retry_guidance": "Add a None check for the 'data' parameter before line 47. The issue reporter explicitly mentions 'passing None causes a crash'."
}
```

This feedback is injected into the agent's retry prompt, giving it specific information about what to fix rather than just "try again."

### Self-Evolution

After the pilot run (50 instances), SIEVE evolves its evaluation components:

| Level | What evolves | How |
|-------|-------------|-----|
| **Rubric text** | LLM-as-judge prompts and criteria | Analyze FP/FN → LLM proposes rubric edits |
| **Semgrep rules** | Static analysis YAML rule files | Analyze FN patterns → LLM generates new rules |

Evolution is validated by re-running on the pilot set (measuring improvement) and on held-out instances (measuring generalization). All evolved artifacts are versioned (`v0/` → `v1/`) for traceability.

## Repo Structure

```
sieve-eval/
├── README.md
├── pyproject.toml
├── sieve/
│   ├── __init__.py
│   ├── pipeline.py              # Main SIEVE orchestration
│   ├── phases/
│   │   ├── __init__.py
│   │   ├── static.py            # Phase 1: syntax, lint, semgrep
│   │   ├── dynamic.py           # Phase 2: existing tests, imports
│   │   └── llm_eval.py          # Phase 3: rubric eval, reproduction check
│   ├── aggregator.py            # Combine signals → verdict + feedback
│   ├── retry.py                 # Retry loop with feedback injection
│   ├── evolution/
│   │   ├── __init__.py
│   │   ├── analyzer.py          # Classify FP/FN from pilot results
│   │   ├── rule_evolver.py      # Evolve Semgrep rules from failure patterns
│   │   └── rubric_evolver.py    # Evolve LLM rubrics from failure patterns
│   └── config.py                # Model configs, thresholds, paths
├── semgrep_rules/
│   ├── v0/                      # Initial hand-written rules
│   │   └── python_patches.yaml
│   └── v1/                      # Rules evolved after pilot (generated)
│       └── python_patches.yaml
├── rubrics/
│   ├── v0.txt                   # Initial evaluation rubric
│   └── v1.txt                   # Evolved rubric after pilot (generated)
├── scripts/
│   ├── run_baseline.py          # Run raw mini-SWE-agent (no SIEVE)
│   ├── run_sieve.py             # Run SIEVE-augmented pipeline
│   ├── run_evolution.py         # Analyze pilot → evolve rules/rubrics
│   └── analyze_results.py       # Generate tables, metrics, case studies
├── data/
│   ├── instances.json           # Selected SWE-bench Verified instances
│   ├── pilot_50/                # 50-instance pilot run outputs
│   │   ├── baseline/            # Raw agent patches + results
│   │   ├── sieve_v0/            # SIEVE v0 patches + verdicts
│   │   ├── sieve_v1/            # SIEVE v1 (evolved) patches + verdicts
│   │   └── failure_taxonomy.json
│   └── scaled/                  # Expanded evaluation outputs
├── notebooks/
│   ├── failure_analysis.ipynb   # Manual failure categorization
│   └── results.ipynb            # Visualization and comparison tables
└── report/
    └── final_report.md
```

## Setup

### Prerequisites

- Python 3.10+
- Docker (for SWE-bench environments)
- Node.js 18+ (for some SWE-bench tooling)

### Installation

```bash
git clone https://github.com/<your-username>/sieve-eval.git
cd sieve-eval
pip install -e ".[dev]"
```

### API Keys

SIEVE uses two LLM providers. Set the following environment variables:

```bash
export OPENAI_API_KEY="..."        # For GPT-5-mini (agent backbone)
export GOOGLE_API_KEY="..."        # For Gemini Flash (evaluator)
```

### SWE-bench Setup

```bash
# Clone and install SWE-bench
pip install swebench

# Pull Docker images for the 50 pilot instances
python scripts/setup_instances.py --split pilot_50
```

## Usage

### 1. Run baseline (raw mini-SWE-agent, no SIEVE)

```bash
python scripts/run_baseline.py \
    --instances data/instances.json \
    --model gpt-5-mini \
    --output data/pilot_50/baseline/
```

### 2. Run SIEVE-augmented pipeline

```bash
python scripts/run_sieve.py \
    --instances data/instances.json \
    --model gpt-5-mini \
    --evaluator gemini-2.0-flash \
    --rubric rubrics/v0.txt \
    --semgrep-rules semgrep_rules/v0/ \
    --max-retries 2 \
    --output data/pilot_50/sieve_v0/
```

### 3. Evolve rules from pilot failures

```bash
python scripts/run_evolution.py \
    --pilot-results data/pilot_50/sieve_v0/ \
    --ground-truth data/pilot_50/baseline/results.json \
    --output-rules semgrep_rules/v1/ \
    --output-rubric rubrics/v1.txt
```

### 4. Re-run with evolved rules

```bash
python scripts/run_sieve.py \
    --instances data/instances.json \
    --model gpt-5-mini \
    --evaluator gemini-2.0-flash \
    --rubric rubrics/v1.txt \
    --semgrep-rules semgrep_rules/v1/ \
    --max-retries 2 \
    --output data/pilot_50/sieve_v1/
```

### 5. Analyze results

```bash
python scripts/analyze_results.py \
    --baseline data/pilot_50/baseline/ \
    --sieve-v0 data/pilot_50/sieve_v0/ \
    --sieve-v1 data/pilot_50/sieve_v1/
```

## Evaluation

### Primary comparison

| Configuration | Pass@1 | Pass@1 (w/ 2 retries) | Avg cost/instance |
|---|---|---|---|
| Mini-SWE-Agent (raw) | —% | N/A | $— |
| + SIEVE v0 (initial rules) | —% | —% | $— |
| + SIEVE v1 (evolved rules) | —% | —% | $— |

### Per-layer analysis

For each cascade layer, we measure what it uniquely catches (patches rejected by this layer that would have passed all other layers) and its false positive rate.

### Evolution effectiveness

We compare v0 → v1 accuracy on the pilot set (measuring improvement) and on held-out instances (measuring generalization), tracking true positive rate, false positive rate, and false negative rate.

## Background & Related Work

SIEVE draws on ideas from several lines of recent work:

- **Patch Reasoner (R4P)** — Reasoning-based patch verification without tests (Xu et al., 2025). Uses pure LLM reasoning; SIEVE adds non-LLM layers and self-evolution.
- **Agentic Rubrics** — Context-aware rubric generation for patch evaluation (2026). Per-instance rubrics; SIEVE evolves rubrics across instances.
- **R2E-Gym** — Hybrid execution-based and execution-free verification (Jain et al., 2025). Demonstrates complementarity; SIEVE decomposes the execution-free side into specific methods.
- **A3-python** — "Kitchen sink" cascade verification from RiSE MSR (Young & Bjørner, 2026). Cascades proof strategies for static bug-finding; SIEVE adapts this for patch verification with self-evolution.
- **Intent Formalization** — Translating informal intent to checkable specs (Lahiri, 2026). SIEVE's rubric extraction from issue descriptions is a lightweight form of intent formalization.
- **SWE-bench+** — Exposed weak test suites in SWE-bench (Aleithan et al., 2025). Motivates non-test evaluation methods.

## Acknowledgments

Course project for [Course Name], Spring 2026, University of Illinois Urbana-Champaign.

## License

MIT

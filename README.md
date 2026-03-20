# EvoEval: Self-Evolving Verification Cascades for Coding Agents

> A verification layer that catches bad patches before submission and provides
> targeted feedback for revision, evaluated on a 50-instance subset of SWE-bench Verified.

## Key Idea

Don't use the LLM to *judge* code. Use the LLM to *generate executable verification
scripts* that run deterministically. The LLM is upstream (building checkers) and
downstream (handling residue), never in the critical evaluation path.

```
  Patch from Agent
        │
        ▼
┌─────────────────────────────────────────┐
│  Layer 1 — Structural Sanity (free)     │ syntax, scope, test contamination
│  Layer 2 — Generated Checkers (cheap)   │ LLM-written Python, runs w/o LLM
│  Layer 3 — LLM Judge Residue (costly)   │ only for ambiguous cases
└────────────────┬────────────────────────┘
                 │
          ACCEPT / REJECT + feedback
                 │
          (retry up to 2×)
```

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/<you>/evoeval.git
cd evoeval
pip install -r requirements.txt

# 2. Set API keys
export TOGETHER_API_KEY="..."     # For Qwen2.5-Coder-32B (agent)
export OPENAI_API_KEY="..."       # For GPT-4o-mini (checker gen + judge)

# 3. Verify setup — run one SWE-bench instance
mini-extra swebench-single \
  --model together_ai/Qwen/Qwen2.5-Coder-32B-Instruct \
  --subset verified --split test -i 0

# 4. Run B0 baseline (pass@1, no verification)
python scripts/run_baseline.py --config b0 --workers 4

# 5. Run EvoEval
python scripts/run_evoeval.py --config e2 --workers 4

# 6. Evaluate
sb-cli submit swe-bench_verified test \
  --predictions_path results/e2/preds.json \
  --run_id evoeval_e2

# 7. Compare
python scripts/analyze_results.py
```

## Project Structure

```
evoeval/
├── docs/
│   ├── EvoEval_project_plan_v0.md   # initial thoughts for this project
│   ├── Implmentation_plan.md        # the TODOs and steps for finishing this project
│   └── mini-swe-agent-analysis.md   # how to use mini-swe-agent
├── config/
│   ├── swebench_qwen.yaml           # Agent config (Qwen2.5-Coder-32B)
│   └── swebench_qwen_feedback.yaml  # Agent config with feedback template
├── data/
│   ├── instance_ids.json            # 50 SWE-bench Verified instance IDs
│   └── split.json                   # 15 evolution / 35 evaluation
├── evoeval/                         # ← The novel contribution
│   ├── orchestrator.py              # L1 → L2 → L3 cascade + retry
│   ├── layer1/                      # Structural sanity checks
│   ├── layer2/                      # LLM-generated checkers
│   ├── layer3/                      # LLM-as-judge residue
│   └── utils/
├── evolution/                       # Meta-prompt evolution loop
├── prompts/                         # All prompt templates
│   ├── checker_meta_prompt_v0.txt   # Seed meta-prompt for Layer 2
│   ├── judge_prompt.txt             # Layer 3 judge prompt
│   └── feedback_template.txt        # Injected into agent on retry
├── scripts/                         # Entry points
├── results/                         # Output per configuration
└── meta_prompts/                    # Evolved meta-prompt versions
```

## Configurations

| ID  | Description                              | Verification | Retries |
|-----|------------------------------------------|-------------|---------|
| B0  | Agent-only, pass@1                       | None        | 0       |
| B1  | Agent-only, pass@3 (random retry)        | None        | 2       |
| B2  | Agent + Layer 1 structural checks        | L1          | 2       |
| B3  | Agent + L1 + seed Layer 2 checkers       | L1+L2       | 2       |
| E1  | Agent + L1 + **evolved** Layer 2         | L1+L2*      | 2       |
| E2  | Agent + L1 + evolved L2 + L3 judge       | L1+L2*+L3   | 2       |

## Model Budget

| Component              | Model                         | Est. Cost   |
|------------------------|-------------------------------|-------------|
| Coding agent           | Qwen2.5-Coder-32B (Together)  | ~$0-15 total |
| Checker generator (L2) | GPT-4o-mini                   | ~$0.50 total |
| LLM judge (L3)         | GPT-4o-mini                   | ~$0.60 total |
| Evolution mutations     | GPT-4o-mini                   | ~$0.50 total |
| **Total**              |                               | **$15-50**   |

## Dataset

We use the [50-instance SWE-bench Verified subset](https://github.com/mariushobbhahn/SWEBench-verified-mini)
split into 15 instances for evolution (meta-prompt tuning) and 35 for held-out evaluation.

## References

- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) — backbone agent
- [SWE-bench Verified](https://www.swebench.com/) — evaluation benchmark
- [sb-cli](https://www.swebench.com/sb-cli/) — cloud evaluation tool

## Citation

```bibtex
@misc{evoeval2026,
  title={EvoEval: Self-Evolving Verification Cascades for Coding Agents},
  author={...},
  year={2026},
  note={Work in progress}
}
```

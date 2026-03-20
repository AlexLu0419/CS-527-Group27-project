# EvoEval: Evolved Verification Cascades for LLM Agent-Generated Code

## Project Plan — Semester-Long Final Project (No External Funding)

---

## 1. Executive Summary

This project builds a verification layer that sits between a coding agent and its patch submission on SWE-bench. Instead of relying on LLM-as-judge (non-deterministic, expensive, opaque), we generate **executable Python checker scripts** per issue — inspired by Microsoft Research's A3-python framework — and organize them in a cost-ordered cascade. A meta-evolution loop improves the *checker generation strategy* over time using ground-truth feedback from a small calibration set.

**Core claim:** LLM-generated, instance-specific verification scripts — organized in a fixed cascade and refined via evolutionary feedback on checker generation prompts — can improve coding agent resolve rates on SWE-bench without access to hidden test suites.

---

## 2. Honest Analysis of the Current Plan

### 2.1 What the Plan Gets Right

**The cascade architecture is validated.** A3-python from MSR (Young & Bjørner, Feb 2026) demonstrates that a fixed, cost-ordered cascade of verification methods works. A3 tries guard detection → 10 barrier certificate types → directed symbolic execution, always in that order, achieving 89–100% false positive elimination on real codebases (requests, DeepSpeed, LLM2CLIP, PyTorch). The key lesson: you don't need an intelligent orchestrator agent to decide which method to use. The cascade *is* the adaptive strategy — cheap methods filter easy cases, expensive methods handle the residue.

**Instance-specific checker generation is the right granularity.** Each SWE-bench issue is unique. A checker evolved on a training set to be broadly useful would learn overly general patterns ("patches should modify files in the traceback") that cannot distinguish correct from incorrect fixes for any specific issue. The A3 approach — synthesizing verification artifacts fresh for each target — maps directly to generating checkers fresh for each issue.

**The meta-evolution framing resolves the generality-vs-specificity tension.** We don't evolve checkers (too specific to generalize). We don't evolve rubrics (too general to help). We evolve the *checker generation pipeline* — the prompts, templates, and strategies that produce instance-specific checkers. This generalizes across issues while remaining specific to each one.

### 2.2 What the Plan Gets Wrong (and Corrections)

**Original mistake: LLM-as-judge as a primary evaluation method.** A3 demonstrates that symbolic/deterministic checks should handle 96%+ of cases. The LLM should only touch the uncertain residue. This reduces cost, increases reproducibility, and produces auditable artifacts. LLM-as-judge is demoted to Layer 3 fallback.

**Original mistake: Adaptive orchestrator agent.** A3 shows a fixed cascade ordered by cost is simpler, more principled, and more effective than an agent choosing among methods. Dropped from the plan.

**Original mistake: Evolving checkers on a training/test split.** Instance-specific checkers cannot generalize across issues. Instead, we evolve the *meta-prompt and template library* that generates checkers. The training set evaluates whether the generation pipeline produces good checkers, not whether the checkers themselves transfer.

**Remaining risk: Generated checkers can be wrong.** An LLM-generated checker that incorrectly rejects a correct patch is worse than no verification. The system must be conservative — when in doubt, accept the patch. False rejections waste retry budgets; false acceptances merely maintain the status quo.

### 2.3 Feasibility Assessment

| Factor | Assessment |
|--------|------------|
| Engineering effort | ~200 hours (15 hrs/week × 14 weeks) |
| API cost | $50–150 total (open-source model for agent, GPT-4o-mini for checker generation) |
| Compute | University GPU or personal machine for local model inference; Docker for SWE-bench |
| SWE-bench subset | 100 instances (30 calibration + 70 evaluation) — standard in the literature |
| Baseline model | mini-SWE-agent + Qwen2.5-Coder-32B or DeepSeek-R1 (open-source, free) |
| Risk of "nothing works" | Low — even basic structural checks improve over no verification |
| Novelty | High — no prior work applies A3-style evolved verification cascades to SWE-bench agent patches |

---

## 3. Architecture

### 3.1 System Overview

```
┌─────────────────────────────────────────────────────────┐
│                    OFFLINE PHASE                         │
│                                                         │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────┐ │
│  │ Calibration  │───▶│ Meta-Prompt  │───▶│ Evolved   │ │
│  │ Set (30 inst)│    │ Evolution    │    │ Templates │ │
│  │ w/ ground    │    │ Loop         │    │ & Prompts │ │
│  │ truth tests  │    │ (3-5 gens)   │    │           │ │
│  └──────────────┘    └──────────────┘    └─────┬─────┘ │
└────────────────────────────────────────────────┼────────┘
                                                 │
┌────────────────────────────────────────────────┼────────┐
│                    ONLINE PHASE                 │        │
│                                                 ▼        │
│  ┌──────────┐    ┌──────────────────────────────────┐   │
│  │ Coding   │───▶│         VERIFICATION CASCADE      │   │
│  │ Agent    │    │                                    │   │
│  │ (mini-   │    │  Layer 1: Structural Filters       │   │
│  │  SWE-    │    │  (free, deterministic, general)    │   │
│  │  agent)  │    │           │                        │   │
│  │          │    │      pass ▼                        │   │
│  │          │    │  Layer 2: Instance-Specific         │   │
│  │          │    │  Executable Checkers                │   │
│  │          │    │  (one LLM call to generate,         │   │
│  │          │    │   then deterministic execution)     │   │
│  │          │    │           │                        │   │
│  │          │    │  inconclusive ▼                    │   │
│  │          │    │  Layer 3: LLM-as-Judge Residue     │   │
│  │          │◀───│  (expensive, last resort)          │   │
│  │ (retry   │    │                                    │   │
│  │  with    │    └──────────────────────────────────┘   │
│  │ feedback)│                                           │
│  └──────────┘                                           │
└─────────────────────────────────────────────────────────┘
```

### 3.2 Layer 1: Structural Sanity Filters

**Purpose:** Catch obviously broken patches using fast, deterministic, zero-cost checks.
**When it runs:** Always, on every patch.
**Cost:** Zero (no model calls, milliseconds to execute).
**Expected catch rate:** ~5–10% of bad patches.

| Check ID | Check | What it catches | Implementation |
|----------|-------|-----------------|----------------|
| L1-SYN | Syntax validity | `py_compile` on all modified files | Syntax errors, incomplete diffs |
| L1-IMP | Import integrity | Verify all imports resolve after patch | Broken dependencies |
| L1-FIL | File relevance | Patch modifies ≥1 file mentioned in issue/traceback | Completely off-target patches |
| L1-TST | Test contamination | Flag if patch modifies `tests/` or `test_*` files | Agent gaming evaluation |
| L1-DEL | Excessive deletion | Flag if deletion:addition ratio > 3:1 | Agent removing code rather than fixing |
| L1-SCP | Scope proportionality | Flag if patch LOC is >10x or <0.1x typical for issue severity | Obvious over/under-engineering |

**Decision logic:**
- Any L1 check returns FAIL → **REJECT** with specific feedback, trigger retry
- All L1 checks pass → proceed to Layer 2

**What evolves:** Thresholds (deletion ratio, scope multiplier) are tuned on calibration set. New structural checks can be proposed by the evolution loop if recurring failure patterns are found.

### 3.3 Layer 2: Instance-Specific Executable Checkers

**Purpose:** Generate and run Python scripts that verify whether THIS patch addresses THIS specific issue.
**When it runs:** Only if Layer 1 passes.
**Cost:** One LLM call to generate checkers (~$0.01–0.05), then free to execute.
**Expected catch rate:** ~10–20% of remaining bad patches.

**Step 2a — Issue analysis and checker generation:**

The system sends a structured prompt to the LLM containing:
1. The GitHub issue description
2. Relevant source files (identified by parsing filenames from the issue/traceback)
3. The agent's patch (git diff)
4. A template library of checker patterns (evolved offline)

The LLM generates 3–5 executable Python functions, each testing a specific property.

**Checker categories (what the template library provides):**

| Category | Example | How it checks |
|----------|---------|---------------|
| Location correctness | "Patch modifies the `send()` method in `adapters.py`" | AST parsing of patched file, verify target function is modified |
| Edge case handling | "Patch adds a guard for `None` input before `.items()` call" | Grep/AST check for guard patterns in the patched function |
| Error handling pattern | "Patch wraps `socket.gaierror` in `ConnectionError`" | AST check for try/except structure with correct exception types |
| API contract preservation | "Patch doesn't change the function's return type" | Compare function signatures before/after patch |
| Regression safety | "Patch doesn't remove existing validation logic" | Diff analysis checking that no assertion/validation lines are deleted without replacement |

**Step 2b — Checker execution:**

Each generated checker is a self-contained Python function that:
- Takes the repo path as input
- Reads/parses source files using `ast` module or string matching
- Returns `True` (property holds), `False` (property violated), or `None` (inconclusive)
- Has a 5-second timeout to prevent hangs

**Step 2c — Aggregation:**

| Result | Decision |
|--------|----------|
| All checkers return True | → **ACCEPT** patch |
| Any checker returns False | → **REJECT** with specific feedback from failed checker(s), trigger retry |
| Mixed True/None, no False | → Proceed to Layer 3 |
| Checker generation failed | → Proceed to Layer 3 |

**Key design principle:** Checkers must be **conservative**. A checker should only return `False` when it has high confidence the patch is wrong. When uncertain, return `None` to defer to Layer 3. This prevents false rejections.

### 3.4 Layer 3: LLM-as-Judge for Residue

**Purpose:** Handle ambiguous cases where Layer 2 is inconclusive.
**When it runs:** Only when Layer 2 produces mixed/inconclusive results (~30–40% of instances).
**Cost:** One LLM call per instance (~$0.02–0.10).
**Expected catch rate:** ~5–10% additional.

**Input to the judge:**
- Issue description
- Patch diff
- Layer 1 analysis results (all passed — that's why we're here)
- Layer 2 checker results with explanations (e.g., "check_wraps_exception: None — could not determine exception type from AST")

**Output:** Accept/Reject with reasoning and confidence level.

**Conservative bias:** When Layer 3 confidence is below a threshold, ACCEPT the patch. The reasoning: rejecting a correct patch wastes a retry attempt, while accepting a wrong patch merely maintains the status quo (the agent would have submitted it anyway without verification).

### 3.5 Meta-Evolution Loop (Offline Phase)

**What evolves:** The checker generation pipeline — not individual checkers.

**Evolved artifacts:**
1. **Meta-prompt for checker generation** — the instructions that tell the LLM how to produce good checkers from an issue description
2. **Template library** — reusable checker patterns organized by issue type (bug fix, feature addition, refactor, edge case handling)
3. **Layer 1 thresholds** — tuned parameters for structural checks
4. **Aggregation policy** — how many checkers must pass/fail to trigger accept/reject

**Evolution procedure:**

```
FOR generation g = 1 to 5:
    FOR each calibration instance i = 1 to 30:
        1. Generate checkers using current meta-prompt + templates
        2. Run checkers on the agent's patch
        3. Compare checker verdict to ground truth (hidden tests)
        4. Record: which checkers were correct? which were wrong?
    
    Compute fitness:
        precision = correct_rejections / total_rejections
        recall = correct_rejections / total_bad_patches  
        score = 2 * precision * recall / (precision + recall)  # F1
        penalty = false_rejections * 2  # penalize false rejections heavily
        fitness = score - penalty
    
    Propose mutations:
        Send evolution results to LLM:
        "Here are the checker generation results from 30 instances.
         These checkers correctly caught bad patches: [examples]
         These checkers incorrectly rejected good patches: [examples]  
         These bad patches were missed: [examples]
         Propose improvements to the meta-prompt and template library."
    
    LLM generates 3-5 variant meta-prompts/templates
    Evaluate each variant on calibration set
    Keep best variant as parent for next generation
```

**Why this avoids the generality problem:** The checkers themselves are always generated fresh per instance. What evolves is the *strategy* for generating them. If generation 1's meta-prompt produces checkers that only check file names, and those checkers miss logic bugs, generation 2's meta-prompt might add instructions like "also check whether the patch handles the specific edge case described in the issue by looking for conditional logic around the mentioned variable." This generalizes because it's a strategy, not a specific check.

---

## 4. Evaluation Plan

### 4.1 Datasets

| Set | Size | Purpose | Ground truth access |
|-----|------|---------|-------------------|
| Calibration set | 30 instances from SWE-bench Verified | Evolve meta-prompts, tune thresholds | Yes (hidden tests available for fitness computation) |
| Evaluation set | 70 instances from SWE-bench Verified | Measure final performance | Yes (but only used at the very end, never during evolution) |
| Optional: SWE-bench Pro subset | 20 instances | Demonstrate generalization to harder tasks | Yes |

Selection: stratified by difficulty (easy/medium/hard based on Lines of code, number of files touched, issue complexity rating from SPICE-Bench).

### 4.2 Baselines

| Baseline | Description |
|----------|-------------|
| B1: Agent alone | mini-SWE-agent with open-source model, no verification layer |
| B2: Agent + simple retry | Agent gets up to 3 attempts with no feedback (mimics pass@3) |
| B3: Agent + LLM-as-judge only | Agent's patch evaluated by a single LLM-as-judge call, rejected patches trigger retry with feedback |
| B4: Agent + static checks only | Agent's patch evaluated by Layer 1 only, rejected patches trigger retry |
| **Ours: Agent + EvoEval** | Full three-layer cascade with evolved checker generation |

### 4.3 Metrics

| Metric | What it measures |
|--------|-----------------|
| Resolve rate (pass@1) | Fraction of instances resolved on first attempt after verification |
| Resolve rate (pass@3) | Fraction resolved within 3 attempts (with verification feedback) |
| True rejection rate | Fraction of correctly rejected bad patches |
| False rejection rate | Fraction of incorrectly rejected good patches (must be very low) |
| Cost per instance | Total LLM API cost for verification (should be << cost of running the agent) |
| Evolution lift | Improvement from evolved meta-prompts vs seed meta-prompt |

### 4.4 Ablation Studies

| Ablation | What it tests |
|----------|---------------|
| Layer 1 only vs full cascade | Value of instance-specific checkers beyond structural checks |
| Seed meta-prompt vs evolved meta-prompt | Value of the evolution loop |
| Layer 2 + Layer 3 vs Layer 2 only | Value of LLM-as-judge residue handling |
| Conservative vs aggressive rejection policy | Impact of false rejection penalty |
| 1 generation vs 3 vs 5 generations | Marginal value of additional evolution |

### 4.5 Expected Results (Honest Predictions)

| Metric | B1 (agent alone) | B3 (LLM judge) | Ours (EvoEval) |
|--------|-------------------|-----------------|----------------|
| Pass@1 (Verified) | 40–55% | 42–57% | 45–60% |
| Pass@3 (Verified) | 50–65% | 53–67% | 55–70% |
| Cost per instance | $0 | $0.05–0.10 | $0.02–0.06 |

The improvement is modest (2–5 percentage points) but meaningful — Live-SWE-agent's self-evolution also adds only 3–5 points. A negative result on evolution lift (evolved prompts ≈ seed prompts) is still publishable if the cascade itself shows improvement.

---

## 5. Implementation Steps and TODOs

### Phase 1: Infrastructure Setup (Weeks 1–3)

- [ ] **Set up SWE-bench environment**
  - Install SWE-bench-Verified evaluation harness
  - Configure Docker containers for isolated execution
  - Verify you can run a baseline agent end-to-end on a single instance
  - Document the setup process (you'll need to reproduce it)

- [ ] **Set up mini-SWE-agent with open-source model**
  - Clone mini-SWE-agent from the Live-SWE-agent repository
  - Configure with Qwen2.5-Coder-32B-Instruct (via Together AI free tier or university GPU with vLLM)
  - Run on 10 test instances to establish a baseline resolve rate
  - Record agent trajectories (patches, reasoning) for analysis

- [ ] **Select and stratify 100 evaluation instances**
  - Download SWE-bench Verified dataset
  - Stratify by: repository, issue type (bug/feature), difficulty, lines changed in gold patch
  - Randomly assign 30 to calibration, 70 to evaluation
  - Verify all 100 instances work in your Docker setup

- [ ] **Install A3-python and run on sample patches**
  - `pip install a3-python`
  - Run `a3 scan` on a few agent-generated patches to understand what it catches
  - Document: what A3 finds that's relevant, what gaps remain for SWE-bench evaluation

**Deliverable:** Working baseline agent with documented resolve rate on 100 instances.

### Phase 2: Build the Verification Cascade (Weeks 4–7)

- [ ] **Implement Layer 1: Structural Filters**
  - Write Python module with 6 structural checks (L1-SYN through L1-SCP)
  - Each check: input = repo path + patch diff, output = pass/fail/warning + explanation
  - Add timeout handling (5 seconds per check)
  - Test on calibration set: how many bad patches does Layer 1 catch alone?
  - Tune thresholds using calibration ground truth

- [ ] **Implement Layer 2: Instance-Specific Checker Generator**
  - Write the meta-prompt (v0 — seed version):
    - Input: issue description, relevant source files, patch diff, template library
    - Output: 3–5 Python checker functions
  - Write the checker execution harness:
    - Apply patch to a temp copy of the repo
    - Execute each checker with timeout
    - Collect results and explanations
  - Write the template library (v0 — seed version):
    - 5 checker templates: location_check, edge_case_check, error_handling_check, api_preservation_check, regression_check
  - Test on calibration set: measure precision/recall of Layer 2

- [ ] **Implement Layer 3: LLM-as-Judge Fallback**
  - Write the judge prompt incorporating Layer 1 + Layer 2 results
  - Implement confidence-based decision logic (low confidence → accept)
  - Test on the cases where Layer 2 is inconclusive

- [ ] **Implement the retry-with-feedback loop**
  - When cascade rejects: extract specific failure reasons from failed checks
  - Format feedback as natural language and inject into agent's next attempt prompt
  - Cap at 3 total attempts per instance

- [ ] **Run full cascade on calibration set, measure end-to-end improvement**

**Deliverable:** Working three-layer cascade with measured improvement over baseline on 30 calibration instances.

### Phase 3: Meta-Evolution Loop (Weeks 8–10)

- [ ] **Implement the evolution framework**
  - Fitness function: F1 of checker verdicts vs ground truth, with 2x penalty for false rejections
  - Mutation operator: send evolution results to LLM, get proposed meta-prompt/template variants
  - Selection: keep top variant per generation

- [ ] **Run evolution for 3–5 generations on calibration set**
  - Generation 1: evaluate seed meta-prompt, collect failure analysis
  - Generation 2–5: propose mutations, evaluate, select best
  - Track: fitness curve, what changes between generations, convergence behavior

- [ ] **Analyze evolution results**
  - What did the evolved meta-prompt learn that the seed didn't have?
  - What checker patterns emerged? What patterns were dropped?
  - Did evolution converge or oscillate?
  - Qualitative examples: a checker that the evolved pipeline generates but the seed doesn't

**Deliverable:** Evolved meta-prompt and template library, with documented fitness improvement over seed.

### Phase 4: Final Evaluation (Weeks 11–12)

- [ ] **Run all baselines on the 70-instance evaluation set**
  - B1: agent alone (pass@1 and pass@3)
  - B2: agent + random retry (pass@3)
  - B3: agent + LLM-as-judge only
  - B4: agent + Layer 1 only

- [ ] **Run EvoEval (seed meta-prompt) on evaluation set**

- [ ] **Run EvoEval (evolved meta-prompt) on evaluation set**

- [ ] **Run ablation studies**
  - Each ablation from Section 4.4

- [ ] **Compute all metrics and statistical significance**
  - Use McNemar's test for pairwise comparison of resolve rates
  - Report confidence intervals

- [ ] **Qualitative case studies**
  - 3 cases where EvoEval correctly caught a bad patch and improved the retry
  - 1–2 cases where EvoEval incorrectly rejected a good patch (failure analysis)
  - 1 case where the evolved meta-prompt generated a checker the seed couldn't

**Deliverable:** Complete experimental results with statistical analysis.

### Phase 5: Paper Writing (Weeks 13–15)

- [ ] **Write the paper**
  - Introduction: motivate with CCC case study + SWE-bench gap
  - Related work: A3-python, Agent-as-a-Judge, Live-SWE-agent, CASTLE
  - Approach: three-layer cascade + meta-evolution
  - Evaluation: baselines, metrics, results, ablations
  - Discussion: when does EvoEval help? when does it hurt? what are the limits?
  - Threats to validity: small dataset, single agent scaffold, checker correctness

- [ ] **Prepare supplementary materials**
  - All evolved meta-prompts and template libraries
  - Example generated checkers for representative instances
  - Full experimental logs

**Target venues:**
- ASE 2026 NIER track (4-page paper, likely May 2026 deadline)
- FSE 2027 Research or IVR track
- ICSE 2027 SEIS track
- AgenticSE workshop at ASE 2026

---

## 6. Key References

### Directly Related Systems

| Reference | Relevance |
|-----------|-----------|
| A3-python (Young & Bjørner, RiSE MSR Blog, Feb 2026) | AI-generated verification cascade for Python; validates our architecture |
| Live-SWE-agent (Xia et al., Nov 2025) | On-the-fly self-evolution for SWE agents; closest methodological precedent |
| Agent-as-a-Judge (Zhuge et al., ICML 2025) | Agentic evaluation framework; our work extends this with executable verification |
| CASTLE (Dubniczky et al., TASE 2025) | Benchmark showing tool combinations outperform individuals; supports cascade design |
| AlphaEvolve (Novikov et al., Google DeepMind, Jun 2025) | Evolutionary code improvement with LLM + automated evaluation; inspires evolution loop |
| ADAS (Hu, Lu, Clune, 2024) | Evolving agent architectures as code; precedent for meta-level evolution |

### SWE-bench Ecosystem

| Reference | Relevance |
|-----------|-----------|
| SWE-bench Verified (OpenAI, 2024) | Primary evaluation benchmark |
| SWE-bench Pro (Deng et al., Scale AI, 2025) | Harder benchmark for generalization testing |
| mini-SWE-agent (Xia et al., 2025) | Our baseline agent scaffold |
| PatchDiff (Wang et al., Mar 2025) | Shows ~29.6% of "passing" patches are actually wrong; motivates verification |

### Formal Methods + LLM Code

| Reference | Relevance |
|-----------|-----------|
| Clover (Sun et al., SAIV 2024) | Generation-verification pipeline for Dafny; 87% acceptance, 0% false positives |
| AutoSpec (Wen et al., CAV 2024) | ACSL specification synthesis; verification of 79% of programs |
| VeriGuard (Miculicich et al., Google Research, Oct 2025) | Formal verification + runtime monitoring for agent safety |
| Zhang et al. (ICML 2025 Position) | Roadmap for LLM + formal methods fusion |
| Intent Formalization (RiSE MSR Blog, Mar 2026) | Formalizing what code should do — the specification gap |

### Self-Evolving Agents

| Reference | Relevance |
|-----------|-----------|
| SICA (Robeyns et al., ICLR Workshop 2025) | Self-improving coding agent via code edits |
| OpenEvolve (open-source AlphaEvolve) | Potential framework for evolution loop implementation |
| Benchmark Self-Evolving (Wang et al., COLING 2025) | Evolving test inputs (we evolve test strategies) |
| Survey on Agent-as-a-Judge (You et al., Jan 2026) | Comprehensive taxonomy of agentic evaluation |

---

## 7. Risk Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Generated checkers are frequently wrong (high false rejection rate) | Medium | High | Conservative aggregation policy; Layer 3 as safety net; heavy penalty for false rejections in fitness function |
| Evolution doesn't improve over seed meta-prompt | Medium | Medium | Still publishable as negative result; the cascade without evolution is a contribution on its own |
| Baseline model too weak (resolve rate <30%) | Low | High | Switch to a stronger open-source model (DeepSeek-V3) or use API credits for Claude Sonnet |
| SWE-bench Docker setup fails on your machine | Low | High | Use university compute cluster; start setup in Week 1 |
| API costs exceed budget | Low | Medium | Use GPT-4o-mini for all generation; use local Qwen for the coding agent; minimize Layer 3 calls |
| 100 instances too few for statistical significance | Medium | Medium | Use McNemar's test (designed for paired binary outcomes on small samples); report effect sizes alongside p-values |

---

## 8. Budget Breakdown

| Item | Cost | Notes |
|------|------|-------|
| Coding agent inference | $0–50 | Qwen2.5-Coder via free Together AI tier or university GPU |
| Checker generation (Layer 2) | $10–30 | GPT-4o-mini, ~$0.01–0.03 per instance × 100 instances × ~5 runs |
| LLM-as-judge (Layer 3) | $5–15 | GPT-4o-mini, only ~30% of instances reach Layer 3 |
| Evolution loop | $20–50 | 5 generations × 30 instances × ~$0.03 per checker generation |
| Miscellaneous (debugging, iteration) | $15–30 | Buffer for failed runs, prompt iteration |
| **Total** | **$50–175** | |

---

## 9. What Not To Attempt

- **Don't build formal verification from scratch.** Use A3-python as-is for inspiration and comparison, not as a component to reimplement.
- **Don't compete on SWE-bench leaderboards.** Labs with frontier models and unlimited compute dominate. Your contribution is the verification method, not the resolve rate.
- **Don't evolve checkers themselves.** They're instance-specific and can't generalize. Evolve the generation pipeline instead.
- **Don't run on full SWE-bench Verified (500 instances).** 100 is sufficient, cheaper, and many papers use subsets.
- **Don't use frontier models as your baseline agent.** Their 70%+ resolve rate leaves little room for improvement. Use an open-source model at 40–55% where there's headroom.
- **Don't implement all 10 of A3's barrier certificate types.** That's a PhD-level formal methods project. Your Layer 2 is a pragmatic approximation using LLM-generated executable scripts.

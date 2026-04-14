# SIEVE: Self-Improving Evaluation via Verification Evolution for SWE Agents

## Project Plan — Graduate Course Final Project (1 Month)

---

## 1. Motivation

LLM-based SWE agents can now resolve 50–75% of SWE-bench Verified issues, but even the best configurations produce **20–40% semantically incorrect patches** — patches that pass all automated validation but are actually wrong (Team Atlanta, 2026). The most dangerous failure modes are invisible to automation: patches that suppress symptoms without fixing root causes, alter unintended functionality, or apply incomplete guards.

The Claude C Compiler incident crystallizes the deeper problem: 16 parallel agents produced 186k lines of Rust that compiled the Linux kernel but couldn't compile "Hello World" — the agents optimized for their test target, not the real world. When ground-truth test suites are unavailable, incomplete, or overfit, we need alternative verification mechanisms.

---

## 2. Honest Assessment of the Landscape

The novelty check revealed that **reproduction-test-from-issues is now a well-established technique** with 12+ published implementations. Key prior systems:

| System | Year | Key Mechanism | SWE-bench Verified |
|--------|------|--------------|-------------------|
| LIBRO | 2023 | First LLM-based bug reproduction from issue reports | — (Defects4J) |
| Agentless | 2024 | 40 candidate repro tests + majority voting | 27.3% |
| SWT-Bench | 2024 | Benchmark formalizing reproduction test generation | — (benchmark) |
| CodeMonkeys | 2025 | Testing State Machine + Editing State Machine, 10 parallel trajectories | 57.4% |
| Otter / e-Otter++ | 2025/2026 | Self-reflective planner + heterogeneous prompting + execution feedback | 63% F→P (TDD-Bench) |
| AEGIS | 2025 | Two-agent (Searcher+Reproducer) with FSM-guided refinement | +19% over baselines |
| InfCode | 2025 | Adversarial co-refinement of tests and patches | 79.4% |
| ExpeRepair | 2025 | Test Agent + Patch Agent + Review Agent + dual memory system | — |
| JoyCode Agent | 2025 | F2P/P2P test gen + closed-loop validate-refine + experience retrieval | 74.6% |
| PatchPilot | 2025 | Reflexion-based PoC reproduction | — |
| R4P (Patch Reasoner) | 2025 | Pure reasoning verification, NO test generation | 72.2% accuracy |
| Agentic Rubrics | 2026 | Structured rubric-based evaluation, NO test generation | 54.2% Best@16 |

**Critical finding:** The "test overfitting" problem is well-documented — LLM-generated verification tests systematically fail to catch incorrect patches that pass them but fail hidden ground-truth tests (Ahmed et al., 2025; R2E-Gym finds <20% of generated tests provide discriminative signal).

### What remains less explored

1. **Combining non-LLM and LLM evaluation in a structured cascade** with explicit cost-ordering (static checks first, tests second, LLM audit third) — most systems use a single verification method
2. **Systematic analysis of per-layer contribution** — which verification method catches which failure type?
3. **The self-evolution of evaluation criteria** (not just prompts) from failure pattern analysis — ExpeRepair's semantic memory is closest but evolves repair strategies, not verification criteria
4. **Integrating trajectory-level behavioral signals** (Li et al., 2026) into the verification decision — no system uses agent behavioral patterns as verification evidence

### Course Project Framing

For a 1-month course project, we do NOT claim to invent reproduction-based verification. Instead, we frame the contribution as:

> **An empirical study of multi-layered verification for SWE agent patches**, combining reproduction testing, static analysis, regression testing, and LLM audit into a cascading pipeline. We measure the marginal contribution of each layer, study how structured feedback improves retry success, and explore whether evaluation criteria can be evolved from observed failure patterns.

This is appropriate for a course project: we build a system, run experiments, and produce insights about what works and why.

---

## 3. Method

### 3.1 Architecture Overview

```
Issue Description
       │
       ▼
┌──────────────────┐
│  Reproduction     │  Gemini Flash reads issue,
│  Agent            │  generates: reproduction script +
│  (separate LLM)   │  expected behavior assertions
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Validate Repro   │  Run on UNPATCHED code
│                   │  Must FAIL → confirms it tests the right bug
│                   │  If PASS → discard, regenerate
└────────┬─────────┘
         │ Validated reproduction test
         ▼
┌──────────────────┐
│  Mini-SWE-Agent   │  GPT-5-mini generates patch
│  (generator LLM)  │  Standard SWE-bench scaffold
└────────┬─────────┘
         │ Candidate patch
         ▼
┌─────────────────────────────────────────────┐
│          SIEVE Evaluation Cascade            │
│                                             │
│  Layer 1 — Static Checks (non-LLM, ~0s):   │
│    • Patch applies cleanly (git apply)      │
│    • Modified files parse (ast.parse)       │
│    • Lint delta (flake8 before/after)       │
│    • Semgrep custom rules                   │
│                                             │
│  Layer 2 — Dynamic Checks (non-LLM, ~30s): │
│    • Reproduction test on PATCHED code      │
│    • Existing repo test suite (regressions) │
│                                             │
│  Layer 3 — LLM Audit (if ambiguous, ~$0.02):│
│    • Does repro script test the right thing?│
│    • Rubric: alignment, scope, completeness │
│                                             │
│  Aggregator → Verdict + Diagnostic Feedback │
└────────────────┬────────────────────────────┘
            ┌────┴────┐
            ▼         ▼
       [ACCEPT]    [REJECT + feedback]
        Submit      → Retry with structured guidance
                    → Up to K=2 retries
```

### 3.2 Reproduction Agent

The reproduction agent receives:
- The issue description (title + body)
- The repository structure (file tree)
- Relevant source files identified by the issue

It generates a standalone Python test file that:
1. Imports the relevant module(s)
2. Sets up the minimal state to trigger the bug
3. Calls the buggy code path
4. Asserts the expected behavior after a correct fix

**Validation:** The script must FAIL on unpatched code (confirming it tests the right bug). Scripts that pass on unpatched code are discarded and regenerated (up to 3 attempts).

**Design choices informed by prior work:**
- Following CodeMonkeys: generate the test WITHOUT codebase context first (forces understanding of the issue semantics), then refine with execution feedback
- Following e-Otter++: use execution feedback to iteratively refine failing scripts
- Unlike Agentless: generate 1 high-quality test with refinement rather than 40 candidates

### 3.3 Evaluation Cascade

The cascade is ordered by cost and determinism. Each layer can REJECT (with feedback), PASS (to next layer), or FLAG (concerns noted but not blocking).

**Layer 1 — Static Checks (cost: ~0, time: <1s)**

| Check | Signal | Action |
|-------|--------|--------|
| `git apply --check` | Patch doesn't apply | REJECT: "Patch has merge conflicts" |
| `ast.parse()` on modified files | Syntax error | REJECT: "SyntaxError at line X" |
| flake8 delta (new errors only) | New lint errors | FLAG if warnings, REJECT if errors |
| Semgrep rules (evolvable) | Anti-pattern match | FLAG with pattern description |

**Layer 2 — Dynamic Checks (cost: ~0 LLM, time: ~30s execution)**

| Check | Signal | Action |
|-------|--------|--------|
| Reproduction test on patched code | PASS/FAIL | REJECT if FAIL: "Repro test still fails: {error}" |
| Existing repo test suite | New failures | REJECT if regressions: "Tests X, Y now fail" |

**Layer 3 — LLM Audit (cost: ~$0.02, only for patches surviving Layer 1-2)**

| Check | Signal | Action |
|-------|--------|--------|
| Repro test quality audit | Does the test actually validate the fix? | FLAG if test is trivial |
| Rubric evaluation | Issue alignment, scope, completeness, regression risk | ACCEPT/REJECT with per-criterion scores |

### 3.4 Feedback and Retry

When SIEVE rejects a patch, it produces structured feedback:

```json
{
  "verdict": "REJECT",
  "layer": "dynamic",
  "details": {
    "reproduction_test": "FAIL — AssertionError at line 12: expected None, got ValueError",
    "regression_tests": "PASS (47/47)",
    "static_checks": "PASS"
  },
  "guidance": "The patch catches the ValueError but returns an incorrect value. The issue expects None to be returned when the input is empty, but your patch returns an empty list instead."
}
```

The retry prompt includes: the original issue, the rejected patch, and the SIEVE feedback. The agent gets up to K=2 retries.

### 3.5 Self-Evolution (Post-Pilot)

After the 50-instance pilot, analyze SIEVE's errors:

**False Negatives (bad patches accepted):**
- Categorize what the reproduction test missed
- Evolve reproduction prompts to generate more edge cases
- Add new Semgrep rules for detected anti-patterns

**False Positives (good patches rejected):**
- Categorize why the reproduction test was too strict
- Evolve reproduction prompts for flexibility
- Adjust Layer 3 rubric thresholds

**Evolution targets:**
| Artifact | How it evolves | Example |
|----------|---------------|---------|
| Reproduction prompt | Add/remove instructions based on failure patterns | "Always test None/empty inputs" |
| Semgrep rules (YAML) | Generate new rules from observed anti-patterns | Detect hardcoded paths |
| LLM audit rubric | Add criteria from observed failure modes | "Check if patch modifies unrelated files" |

---

## 4. Experimental Design

### 4.1 Comparison

| Configuration | Description |
|---------------|-------------|
| **Baseline** | Raw mini-SWE-agent, Pass@1 |
| **+Repro** | Add reproduction test generation + validation, no retry |
| **+Repro+Retry** | Add feedback-guided retry (up to K=2) |
| **+SIEVE-v0** | Full cascade (static + repro + regression + LLM audit) + retry |
| **+SIEVE-v1** | Evolved cascade after pilot analysis |

### 4.2 Metrics

**Primary:**
- Pass@1 (% of instances resolved)
- Pass@1-with-retries (after up to K=2 SIEVE-guided retries)

**Diagnostic:**
- Reproduction test quality: F→P rate (% of repro tests that correctly fail-then-pass)
- Per-layer catch rate: what % of incorrect patches does each layer uniquely catch?
- False positive rate: what % of correct patches does SIEVE incorrectly reject?
- Retry improvement rate: when SIEVE rejects and provides feedback, how often does retry succeed?
- Cost per instance: total $ (LLM calls) and time (execution)

### 4.3 Instance Selection

50 instances from SWE-bench Verified, stratified by:
- Repository (diverse projects)
- Difficulty (estimated by historical agent success rates)
- Issue type (bug fix, feature, edge case)

Hold out 10 of the 50 for evolution generalization testing.

### 4.4 Analyses

1. **Layer contribution table:** For each cascade layer, report unique catches, false positives, cost
2. **Reproduction test quality:** What fraction of generated tests correctly fail-then-pass? What fraction are trivial?
3. **Feedback quality:** When SIEVE rejects, does the feedback lead to better retries vs. blind retry?
4. **Evolution effectiveness:** Do evolved rules improve on held-out instances?
5. **Case studies (3-5):** Concrete examples of SIEVE catching a real problem, missing one, and evolving

---

## 5. References

### Reproduction Test Generation for SWE-bench

- **LIBRO** — Kang et al., "Large Language Models are Few-Shot Testers: Exploring LLM-Based General Bug Reproduction," ICSE 2023
- **Agentless** — Xia et al., "Agentless: Demystifying LLM-based Software Engineering Agents," arXiv:2407.01489, 2024
- **SWT-Bench** — Mündler et al., "SWT-Bench: Testing and Validating Real-World Bug-Fixes with Code Agents," NeurIPS 2024
- **TDD-Bench Verified** — Ahmed et al., "TDD-Bench Verified: Can LLMs Generate Tests for Issues Before They Get Resolved?," arXiv:2412.02883, 2024
- **CodeMonkeys** — Ehrlich et al., "CodeMonkeys: Scaling Test-Time Compute for Software Engineering," arXiv:2501.14723, 2025
- **Otter / e-Otter++** — Ahmed et al., "Otter: Generating Tests from Issues to Validate SWE Patches," arXiv:2502.05368, 2025; "Heterogeneous Prompting and Execution Feedback for SWE Issue Test Generation and Selection," arXiv:2508.06365, 2025
- **AEGIS** — Wang et al., "AEGIS: An Agent-based Framework for Bug Reproduction from Issue Descriptions," FSE 2025
- **Issue2Test** — Nashid et al., "Issue2Test: Generating Reproducing Test Cases from Issue Reports," arXiv:2503.16320, 2025
- **Echo** — Fei et al., "Echo: Graph-Enhanced Retrieval and Execution Feedback for Issue Reproduction Test Generation," arXiv:2603.07326, 2026

### Patch Verification Without Ground-Truth Tests

- **R4P (Patch Reasoner)** — Xu et al., "Scalable Supervising Software Agents with Patch Reasoner," arXiv:2510.22775, 2025
- **Agentic Rubrics** — Raghavendra et al., "Agentic Rubrics as Contextual Verifiers for SWE Agents," arXiv:2601.04171, 2026
- **R2E-Gym** — Jain et al., "R2E-Gym: Procedural Environments and Hybrid Verifiers for Scaling Open-Weights SWE Agents," arXiv:2504.07164, 2025
- **InfCode** — Li et al., "InfCode: Adversarial Iterative Refinement of Tests and Patches for Reliable Software Issue Resolution," arXiv:2511.16004, 2025
- **Abstain and Validate** — Cambronero et al., "Abstain and Validate: A Dual-LLM Policy for Reducing Noise in Agentic Program Repair," arXiv:2510.03217, 2025

### Integrated Repair Agents with Verification

- **ExpeRepair** — "ExpeRepair: Dual-Memory Enhanced LLM-based Repository-Level Program Repair," arXiv:2506.10484, 2025
- **JoyCode Agent** — JD.com, Repository-level Repair Agent, github.com/jd-opensource/joycode-agent, 2025
- **PatchPilot** — Li et al., "PatchPilot: A Cost-Efficient Software Engineering Agent with Early Attempts on Formal Verification," arXiv:2502.02747, 2025
- **SWE-SynInfer+** — Ma et al., "Thinking Longer, Not Larger: Enhancing Software Engineering Agents via Scaling Test-Time Compute," arXiv:2503.23803, 2025
- **Live-SWE-Agent** — Xia et al., "Live-SWE-agent: Can Software Engineering Agents Self-Evolve on the Fly?," arXiv:2511.13646, 2025

### Test Overfitting and Patch Correctness

- **Test overfitting on SWE-bench** — "Investigating Test Overfitting on SWE-bench," arXiv:2511.16858, 2025
- **ORACLE-SWE** — "ORACLE-SWE: Quantifying the Contribution of Oracle Information Signals on SWE Agents," arXiv:2604.07789, 2026
- **UTBoost** — Yu et al., "UTBoost: Rigorous Evaluation of Coding Agents on SWE-bench," arXiv:2506.09289, 2025
- **SWE-bench+** — Aleithan et al., "SWE-bench+: Enhanced Coding Benchmark for LLMs," ICLR 2026

### Patch Ensemble and Behavioral Analysis

- **Team Atlanta Patch Ensemble** — Zhang et al., "Patching Vulnerabilities with Coding Agents in 2026," team-atlanta.github.io, 2026
- **413K Trajectory Analysis** — Li et al., "We Analyzed 413K AI Agent Runs. Here's What Separates the Ones That Succeed," hanchenli.github.io, 2026

### Agent-Synthesized Test Generators and Verification

- **Gentoo** — Vikram & Padhye, "Fuzzing with Agents? Generators Are All You Need," arXiv:2604.01442, 2026
- **Gym-Anything** — Aggarwal, Neubig, Welleck, "Gym-Anything: Turn any Software into an Agent Environment," arXiv:2604.06126, 2026
- **A3-python** — Young & Bjørner, "How to train your program verifier," RiSE MSR Blog, 2026
- **Intent Formalization** — Lahiri, "Intent Formalization: A Grand Challenge for Reliable Coding in the Age of AI Agents," RiSE MSR Blog, 2026

### Broader APR Patch Validation

- **Patch Correctness Assessment Survey** — ACM TOSEM, 2025
- **FIXCHECK** — Molina et al., "Improving Patch Correctness Analysis via Random Testing and LLMs," ICST 2024
- **DiffTGen** — Xin & Reiss, "Identifying Test-Suite-Overfitted Patches through Test Case Generation," ISSTA 2017

---

## 6. Week-by-Week Plan

### Week 1: Infrastructure + Baseline + Reproduction Agent (Days 1–7)

**Days 1–2: SWE-bench infrastructure**
- Clone SWE-bench repo, configure Docker
- Select 50 instances (stratified), verify containers build
- Set up mini-SWE-agent scaffold with GPT-5-mini

**Days 3–4: Baseline runs**
- Run raw mini-SWE-agent on 50 instances
- Record: patches, pass/fail, agent trajectories
- This gives the baseline Pass@1

**Days 5–7: Reproduction agent**
- Prompt engineering for Gemini Flash reproduction test generation
- Test on 10 instances: what fraction of generated tests correctly fail on unpatched code?
- Iterate prompt until F→P validation rate is reasonable (target: >50%)
- Record reproduction test quality metrics

**Deliverable:** Baseline numbers + working reproduction agent with measured F→P rate

### Week 2: SIEVE Cascade + Feedback Loop (Days 8–14)

**Days 8–9: Phase 1 — Static checks**
- Implement: patch validity, AST parse check, flake8 delta, initial Semgrep rules
- Test on baseline patches: how many bad patches does Phase 1 catch?

**Days 10–11: Phase 2 — Dynamic checks**
- Integrate reproduction test execution into cascade
- Integrate existing repo test suite execution
- Test: how many additional bad patches does Phase 2 catch beyond Phase 1?

**Days 12–13: Phase 3 — LLM audit + aggregator**
- Implement rubric-based LLM evaluation (Gemini Flash)
- Build aggregator: combine signals → verdict + structured feedback
- Build feedback injection into retry prompt

**Day 14: Full pipeline run**
- Run complete SIEVE pipeline on 50 instances with K=2 retries
- Record: per-layer verdicts, feedback messages, retry outcomes

**Deliverable:** Working SIEVE-v0 pipeline with full results on 50 instances

### Week 3: Self-Evolution + Extended Evaluation (Days 15–21)

**Days 15–16: Failure analysis**
- Classify all SIEVE-v0 errors: false positives, false negatives
- Build failure taxonomy (what types of errors does each layer miss?)
- Identify top 3–5 failure patterns to address

**Days 17–18: Evolution**
- Evolve reproduction prompts from false negative patterns
- Generate new Semgrep rules from observed anti-patterns
- Adjust rubric criteria based on false positive analysis
- Re-run evolved SIEVE-v1 on 40 pilot instances

**Days 19–20: Generalization check**
- Run SIEVE-v1 on 10 held-out instances
- Optionally: run on additional 50–100 instances if budget permits

**Day 21: Collect all results**
- Compile comparison tables across all configurations

**Deliverable:** SIEVE-v1 with evolution metrics, generalization results

### Week 4: Analysis + Report (Days 22–30)

**Days 22–24: Quantitative analysis**
- Build comparison tables (baseline vs. +Repro vs. +SIEVE-v0 vs. +SIEVE-v1)
- Per-layer contribution analysis
- Cost analysis ($ and time per configuration)
- Reproduction test quality analysis

**Days 25–27: Qualitative analysis**
- Write 3–5 detailed case studies
- Create visualizations (cascade flow, layer contribution chart)
- Analyze evolved artifacts (show specific Semgrep rules, rubric changes)

**Days 28–30: Report writing**
- 8–12 page report: motivation, method, results, analysis, limitations, conclusion
- Clean up code, push to GitHub

**Deliverable:** Final report + clean GitHub repo

---

## 7. Budget

| Item | Cost |
|------|------|
| GPT-5-mini: 50 instances baseline (~$0.50/instance) | ~$25 |
| GPT-5-mini: 50 instances × 2 retries (~$0.50/retry) | ~$50 |
| Gemini Flash: reproduction generation (50 × ~$0.02) | ~$1 |
| Gemini Flash: LLM audit (50 × 3 attempts × ~$0.02) | ~$3 |
| Extended run (50–100 more instances) | ~$100 |
| Evolution LLM calls | ~$5 |
| Buffer for debugging/reruns | ~$50 |
| **Total** | **~$235** |

---

## 8. Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| SWE-bench Docker setup is painful | Start Day 1; fall back to SWE-bench Lite if needed |
| Reproduction tests have low F→P rate | Iterate prompts in Week 1; even 30% is usable |
| SIEVE adds cost but no improvement | Per-layer analysis is still a valid contribution |
| Self-evolution overfits to pilot | Hold out 10 instances for generalization check |
| Budget overrun | Use Gemini Flash-Lite for audit; reduce retry count |

---

## 9. What "Success" Looks Like

**Minimum (passing grade):**
- SIEVE pipeline works end-to-end on 50 instances
- Clear per-layer analysis showing what each check catches
- Honest discussion of what works and what doesn't

**Good (A):**
- Measurable improvement over baseline (even 2–3 percentage points)
- Self-evolution demonstrably improves on pilot
- 3+ compelling case studies

**Great (A+ / potential workshop paper seed):**
- Evolved rules generalize to held-out instances
- Clear evidence that structured feedback beats blind retry
- Insightful analysis of WHY certain layers catch certain failure types
- Reproduction test quality analysis reveals actionable patterns

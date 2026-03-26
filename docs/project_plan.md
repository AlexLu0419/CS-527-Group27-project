# SIEVE: Self-Improving Evaluation via Verification Evolution

**Repo name:** `sieve-eval`
**Full title:** SIEVE: Self-Improving Evaluation via Verification Evolution for SWE Agents

The metaphor: a sieve filters out bad patches before they hit the (expensive) ground-truth test suite, and the mesh gets finer over time as the system learns from its mistakes.

---

## Project Scope (1 Month)

**One-sentence pitch:** SIEVE adds a multi-method evaluation layer above a mini-SWE-agent that catches failed patches before test execution, provides structured feedback for retry, and evolves its evaluation rules from observed failures.

**Comparison:** Raw mini-SWE-agent (Pass@1) vs. SIEVE-augmented agent (Pass@1 after up to K retries guided by SIEVE feedback).

**Benchmark:** SWE-bench Verified, 50-instance pilot → full 500 (or as many as budget allows).

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                    SIEVE Pipeline                    │
│                                                     │
│  Issue ──→ Mini-SWE-Agent ──→ Candidate Patch       │
│              (GPT-5-mini)           │                │
│                                     ▼                │
│                          ┌──────────────────┐       │
│                          │  Phase 1: Static  │       │
│                          │  (Non-LLM, fast)  │       │
│                          └────────┬─────────┘       │
│                     PASS/FLAG     │    REJECT→feedback│
│                                   ▼                  │
│                          ┌──────────────────┐       │
│                          │  Phase 2: Dynamic │       │
│                          │  (Non-LLM, mod.)  │       │
│                          └────────┬─────────┘       │
│                     PASS/FLAG     │    REJECT→feedback│
│                                   ▼                  │
│                          ┌──────────────────┐       │
│                          │  Phase 3: LLM     │       │
│                          │  (Gemini Flash)   │       │
│                          └────────┬─────────┘       │
│                   ACCEPT/REJECT   │                  │
│                                   ▼                  │
│                          ┌──────────────────┐       │
│                          │  Aggregator       │       │
│                          │  Verdict+Feedback │       │
│                          └────────┬─────────┘       │
│                                   │                  │
│                    ┌──────────────┴──────────┐      │
│                    ▼                         ▼      │
│              [ACCEPT]                 [REJECT]      │
│              Submit patch       Feedback → Retry    │
│                                 (up to K times)     │
│                                                     │
│  ── Self-Evolution Loop ──                          │
│  After pilot: analyze failures → evolve rules       │
└─────────────────────────────────────────────────────┘
```

---

## Week-by-Week Plan

### Week 1: Infrastructure + Baseline Generation (Days 1-7)

**Goal:** Get mini-SWE-agent running, generate baseline patches on 50 instances, collect raw Pass@1.

**Tasks:**

1. **Set up SWE-bench Verified environment**
   - Clone swebench repo, set up Docker infrastructure
   - Select 50 instances (stratified: mix of easy/medium/hard, diverse repos)
   - Verify Docker containers build and tests run

2. **Set up mini-SWE-agent**
   - Use the Epoch AI / SWE-bench team's mini-SWE-agent scaffold (bash-only)
   - Configure with GPT-5-mini as backbone
   - Run on 50 instances, save: patches, agent trajectories, pass/fail results
   - This gives you the **baseline Pass@1**

3. **Manual failure analysis (critical!)**
   - For each failed patch, manually categorize WHY it failed
   - Build initial failure taxonomy:
     - Syntax/parse errors
     - Wrong file edited
     - Incomplete fix (addresses part of issue)
     - Wrong approach (misunderstands the issue)
     - Regression (breaks existing functionality)
     - Edge case missed
     - Environment/import errors
   - This taxonomy drives everything that follows

**Deliverable:** Baseline numbers + annotated failure taxonomy on 50 instances.

---

### Week 2: Build the Evaluation Cascade (Days 8-14)

**Goal:** Implement Phase 1-3 of SIEVE and the feedback mechanism.

#### Phase 1: Static Checks (Non-LLM)

**Layer 1.1 — Patch Validity**
```python
# Does the patch apply cleanly?
# Does the patched code parse?
# Are there new syntax errors?
def check_patch_validity(repo_path, patch_diff):
    # 1. Apply patch with `git apply --check`
    # 2. Run `python -m py_compile` on modified files
    # 3. Run `python -c "import ast; ast.parse(open(f).read())"` 
    # Return: PASS / REJECT + specific error message
```

**Layer 1.2 — Static Analysis Delta**
```python
# Run lightweight checks before and after patch
def check_static_delta(repo_path, patch_diff):
    # 1. Run pylint/flake8 on modified files BEFORE patch
    # 2. Apply patch
    # 3. Run pylint/flake8 on modified files AFTER patch
    # 4. Compare: new errors = bad signal, fewer errors = good signal
    # Return: PASS / FLAG(new_warnings) / REJECT(new_errors)
```

**Layer 1.3 — Semgrep Rules (Evolvable)**
```yaml
# Initial rules (starter set, will be evolved)
rules:
  - id: hardcoded-version-path
    pattern: '"/usr/lib/gcc/..."'  # The CCC pattern
    message: "Hardcoded version-specific path detected"
  - id: bare-except
    pattern: 'except: ...'
    message: "Bare except clause may hide errors"
  - id: unused-import-in-patch
    # Flag imports added by patch but never used
```

**Implementation:** Write a `semgrep_rules/` directory with YAML rules. Easy to add new rules later (this is the evolvable part).

#### Phase 2: Dynamic Checks (Non-LLM)

**Layer 2.1 — Existing Test Suite**
```python
# Run the repo's own test suite (NOT ground-truth tests)
def check_existing_tests(repo_path, patch_diff):
    # 1. Identify the project's test runner (pytest, unittest, etc.)
    # 2. Run tests on patched code
    # 3. Compare against pre-patch test results
    # Return: PASS / REJECT(test_failures) with failure output
```

This is the most powerful non-LLM check. Many SWE-bench repos have extensive test suites beyond the hidden ground-truth tests. A patch that breaks existing tests is almost certainly wrong.

**Layer 2.2 — Import/Dependency Check**
```python
# Verify all imports resolve and no circular dependencies introduced
def check_imports(repo_path, patch_diff):
    # 1. Extract new imports from patch
    # 2. Verify they exist in the environment
    # 3. Check for circular import issues
```

#### Phase 3: LLM-Based Evaluation

**Layer 3.1 — Rubric-Based Evaluation (Evolvable)**
```python
INITIAL_RUBRIC = """
Evaluate this patch against the following criteria:

1. ISSUE ALIGNMENT: Does the patch address the specific problem described 
   in the issue? (Not a different problem, not a partial fix)
2. CORRECTNESS: Does the logic of the change appear correct? Are edge 
   cases handled?
3. SCOPE: Does the patch modify only what's necessary? Are there 
   unnecessary changes?
4. REGRESSION RISK: Could this change break existing functionality?
5. COMPLETENESS: Does the patch handle all aspects of the issue, 
   including error cases mentioned?

For each criterion, rate: PASS / CONCERN / FAIL
Provide a brief explanation for each rating.
Final verdict: ACCEPT / REJECT
If REJECT, provide specific feedback for the developer to fix the patch.
"""

def llm_evaluate(issue_desc, patch_diff, relevant_code, rubric):
    # Call Gemini Flash with: issue + patch + surrounding code + rubric
    # Parse structured output
    # Return: verdict + per-criterion scores + feedback
```

**Layer 3.2 — Issue Reproduction Reasoning**
```python
# Ask LLM: "Given this issue and this patch, would the original 
# bug still be reproducible?"
def llm_reproduction_check(issue_desc, patch_diff, relevant_code):
    # Focused prompt: trace through the bug scenario with the patch applied
    # Return: LIKELY_FIXED / UNCERTAIN / LIKELY_NOT_FIXED + reasoning
```

#### Aggregator + Feedback Generator

```python
def aggregate_and_decide(phase1_results, phase2_results, phase3_results):
    # Hard rejects: any phase returns REJECT with high confidence
    # Soft signals: aggregate FLAGS and CONCERNs
    # Generate structured feedback for retry:
    feedback = {
        "verdict": "REJECT",
        "confidence": 0.85,
        "reasons": [
            "Phase 1: New flake8 error E302 in models.py line 47",
            "Phase 3: Rubric criterion 'COMPLETENESS' failed — patch 
             handles the main case but misses the edge case where 
             input is None, which is explicitly mentioned in the issue"
        ],
        "retry_guidance": "The patch needs to add a None check for the 
         'data' parameter before processing. See the issue description 
         paragraph 2 where the reporter mentions 'passing None causes...'",
    }
    return feedback
```

#### Retry Loop

```python
def sieve_pipeline(issue, repo_path, max_retries=2):
    for attempt in range(max_retries + 1):
        if attempt == 0:
            patch = mini_swe_agent.generate(issue, repo_path)
        else:
            # Augment prompt with SIEVE feedback
            patch = mini_swe_agent.generate(
                issue, repo_path, 
                previous_patch=prev_patch,
                feedback=sieve_feedback
            )
        
        sieve_result = evaluate_cascade(issue, repo_path, patch)
        
        if sieve_result.verdict == "ACCEPT":
            return patch  # Submit this one
        
        prev_patch = patch
        sieve_feedback = sieve_result.feedback
    
    # After max retries, submit best patch (highest confidence)
    return best_patch_by_confidence
```

**Deliverable:** Working SIEVE cascade that takes (issue, patch) → (verdict, feedback). Retry loop integrated with mini-SWE-agent.

---

### Week 3: Self-Evolution + Scaled Evaluation (Days 15-21)

**Goal:** Evolve evaluation rules from pilot failures, then run on larger set.

#### Self-Evolution Protocol

**Step 1: Analyze pilot results**

After running SIEVE on 50 instances (with retries), categorize outcomes:
- True Positives: SIEVE rejected a bad patch (good!)
- True Negatives: SIEVE accepted a good patch (good!)
- False Positives: SIEVE rejected a CORRECT patch (bad — wasted a retry)
- False Negatives: SIEVE accepted a BAD patch (bad — missed an error)

**Step 2: Evolve from false negatives (patches SIEVE missed)**

For each false negative, ask: "What check COULD have caught this?"

```python
# Use LLM to propose new Semgrep rules from failure patterns
def evolve_semgrep_rules(false_negative_patches, failure_analyses):
    prompt = f"""
    These patches were accepted by our evaluation but turned out to be wrong.
    For each, here's what was wrong:
    {failure_analyses}
    
    Propose new Semgrep YAML rules that would catch these patterns.
    Each rule should be general enough to apply beyond this specific case.
    """
    # Parse output → new .yaml rule files
```

```python
# Evolve the rubric from observed failure patterns  
def evolve_rubric(false_negatives, false_positives, current_rubric):
    prompt = f"""
    Current evaluation rubric:
    {current_rubric}
    
    Cases where the rubric MISSED a bad patch:
    {false_negatives}
    
    Cases where the rubric WRONGLY rejected a good patch:
    {false_positives}
    
    Propose an improved rubric that would catch the missed cases 
    while reducing false rejections. Add specific criteria learned 
    from these failure patterns.
    """
    # Return: evolved rubric text
```

**Step 3: Validate evolution**

Re-run evolved SIEVE on the same 50 instances. Measure:
- Did false negatives decrease?
- Did false positives increase? (overfitting risk)
- Net improvement?

**Step 4: Scale up**

Run evolved SIEVE on additional instances (100-200 more, budget permitting). This tests whether evolved rules generalize beyond the pilot set.

**Deliverable:** Evolved rule set + before/after metrics on pilot + results on expanded set.

---

### Week 4: Analysis + Writeup (Days 22-30)

**Goal:** Comprehensive analysis and project report.

#### Key analyses to include

1. **Baseline comparison table:**

| Metric | Mini-SWE-Agent (raw) | + SIEVE (no evolution) | + SIEVE (evolved) |
|--------|---------------------|----------------------|-------------------|
| Pass@1 | X% | Y% | Z% |
| Pass@1 (with 2 retries) | N/A | Y'% | Z'% |
| Avg. LLM cost per instance | $A | $B | $C |

2. **Per-layer contribution analysis:**

For each SIEVE layer, report:
- How many patches it caught (that later layers didn't need to catch)
- False positive rate
- Cost (time, tokens)

This justifies the cascade — if a layer catches nothing unique, it's dead weight.

3. **Evolution effectiveness:**

| Metric | Before Evolution | After Evolution |
|--------|-----------------|-----------------|
| True Positive Rate | X% | Y% |
| False Positive Rate | X% | Y% |
| False Negative Rate | X% | Y% |

Plus: do evolved rules generalize to unseen instances? (Critical question)

4. **Qualitative case studies (3-5 examples):**
- A patch that SIEVE correctly rejected with useful feedback, leading to a successful retry
- A patch that SIEVE incorrectly rejected (false positive) — what went wrong?
- A failure pattern that evolution successfully addressed
- A failure pattern that remains unsolved

5. **Evolved artifacts showcase:**

Show the actual evolved Semgrep rules and rubric changes. For example:
- "After seeing 3 patches that forgot to handle `None` inputs, SIEVE evolved a rubric criterion: 'Check if the patch handles None/empty inputs for all modified function parameters'"
- "After seeing a patch that imported a module only available in Python 3.10+, SIEVE evolved a Semgrep rule detecting version-specific stdlib imports"

**Deliverable:** Project report (8-12 pages) + GitHub repo with all code, data, and results.

---

## Repo Structure

```
sieve-eval/
├── README.md
├── pyproject.toml
├── sieve/
│   ├── __init__.py
│   ├── pipeline.py          # Main SIEVE orchestration
│   ├── phases/
│   │   ├── __init__.py
│   │   ├── static.py        # Phase 1: syntax, lint, semgrep
│   │   ├── dynamic.py       # Phase 2: existing tests, imports
│   │   └── llm_eval.py      # Phase 3: rubric eval, reproduction
│   ├── aggregator.py        # Combine signals → verdict + feedback
│   ├── retry.py             # Retry loop with feedback injection
│   ├── evolution/
│   │   ├── __init__.py
│   │   ├── analyzer.py      # Analyze FP/FN from pilot
│   │   ├── rule_evolver.py  # Evolve Semgrep rules
│   │   └── rubric_evolver.py # Evolve LLM rubrics
│   └── config.py            # Model configs, thresholds
├── semgrep_rules/
│   ├── v0/                  # Initial rules
│   │   └── python_patches.yaml
│   └── v1/                  # Evolved rules (after pilot)
│       └── python_patches.yaml
├── rubrics/
│   ├── v0.txt               # Initial rubric
│   └── v1.txt               # Evolved rubric
├── scripts/
│   ├── run_baseline.py      # Run raw mini-SWE-agent
│   ├── run_sieve.py         # Run SIEVE pipeline
│   ├── run_evolution.py     # Run evolution loop
│   └── analyze_results.py   # Generate analysis tables
├── data/
│   ├── pilot_50/            # 50-instance pilot data
│   │   ├── instances.json   # Selected SWE-bench instances
│   │   ├── baseline_patches/ 
│   │   ├── sieve_patches/
│   │   ├── failure_taxonomy.json
│   │   └── results.json
│   └── scaled/              # Expanded evaluation data
├── notebooks/
│   ├── failure_analysis.ipynb
│   └── results_visualization.ipynb
└── report/
    └── final_report.md
```

---

## Budget Estimate

| Item | Cost |
|------|------|
| GPT-5-mini: 50 instances × ~$0.50/instance (baseline) | ~$25 |
| GPT-5-mini: 50 instances × 2 retries × ~$0.50 (SIEVE runs) | ~$50 |
| Gemini Flash: 50 instances × 3 eval calls × ~$0.02 | ~$3 |
| Scaled run (200 instances with SIEVE) | ~$150 |
| Evolution LLM calls | ~$10 |
| Buffer for debugging/reruns | ~$60 |
| **Total** | **~$300** |

Gemini Flash-Lite for the evaluator keeps LLM evaluation costs near-negligible. The main cost is the mini-SWE-agent runs themselves.

---

## What "Success" Looks Like for a Course Project

**Minimum viable result (B+ grade):**
- SIEVE pipeline works end-to-end on 50 instances
- Clear per-layer analysis showing what each check catches
- At least marginal improvement over raw baseline with retry

**Good result (A grade):**
- Measurable Pass@1 improvement (even 2-3 percentage points is meaningful)
- Self-evolution demonstrably improves cascade accuracy on pilot
- 3+ compelling case studies showing SIEVE catching real problems

**Great result (A+ / publishable seed):**
- Evolved rules generalize to unseen instances
- Clear evidence that structured feedback produces better retry patches than blind retry
- Failure taxonomy itself is insightful and well-documented

---

## Risk Mitigation

**Risk: Mini-SWE-agent is too good (few failures to analyze)**
- Mitigation: GPT-5-mini with a simple scaffold should have plenty of failures. Current mini-SWE-agents with 7B models get ~25-30% on SWE-bench Verified. Even with GPT-5-mini, expect 50-65% at best, giving you 20-25 failures out of 50 to work with.

**Risk: SIEVE adds cost but no improvement**
- Mitigation: Even if Pass@1 doesn't improve, the per-layer analysis ("static checks catch X%, LLM catches Y%") is still a valid contribution for a course project. The analysis of what different methods CAN and CANNOT catch is interesting regardless.

**Risk: Self-evolution overfits to pilot**
- Mitigation: Hold out 10 of the 50 instances. Evolve on 40, test on 10. Small sample but demonstrates the methodology.

**Risk: Docker/SWE-bench infrastructure is painful to set up**
- Mitigation: Use the official SWE-bench Docker images. If infrastructure is too painful, fall back to running on SWE-bench Lite (300 instances, simpler setup). Start infrastructure setup on Day 1.

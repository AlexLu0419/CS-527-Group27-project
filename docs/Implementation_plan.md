# EvoEval: Self-Evolving Verification Cascades for Coding Agents

## Implementation Plan & TODOs

---

## 1. Project Overview

**Goal:** Build a verification layer that sits between a coding agent and patch submission on SWE-bench Verified. The system uses a three-layer cascade of checks — structural filters, instance-specific generated checkers, and LLM-as-judge residue — to catch bad patches before submission and provide targeted feedback for revision.

**Key insight (from A3-python / RiSE MSR):** Don't use the LLM to *judge* code. Use the LLM to *generate executable verification scripts* that run deterministically. The LLM is upstream (building checkers) and downstream (handling residue), never in the critical evaluation path.

**Evaluation benchmark:** 50-instance subset of SWE-bench Verified.

**Backbone agent:** mini-SWE-agent (open-source, ~100 lines Python).

**LLM brain:** Smaller/older models (see Section 3 for details).

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    SWE-bench Instance                    │
│         (issue description + codebase snapshot)          │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              mini-SWE-agent + LLM brain                 │
│         Generates candidate patch (git diff)            │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                 EVOEVAL VERIFICATION                     │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │ LAYER 1: Structural Sanity (free, instant)       │   │
│  │  - Syntax validity (py_compile)                  │   │
│  │  - File relevance check                          │   │
│  │  - Test file contamination                       │   │
│  │  - Import integrity                              │   │
│  │  - Scope proportionality                         │   │
│  │  - Deletion-without-replacement                  │   │
│  └────────────────────┬─────────────────────────────┘   │
│           FAIL → REJECT + feedback to agent             │
│           PASS ↓                                        │
│  ┌──────────────────────────────────────────────────┐   │
│  │ LAYER 2: Instance-Specific Checkers (cheap)      │   │
│  │  - LLM generates 3-5 Python checker functions    │   │
│  │    based on THIS issue + THIS codebase           │   │
│  │  - Checkers run deterministically (no LLM)       │   │
│  │  - Results: pass/fail per checker + reasons       │   │
│  └────────────────────┬─────────────────────────────┘   │
│           ALL PASS → ACCEPT patch                       │
│           MIXED ↓                                       │
│  ┌──────────────────────────────────────────────────┐   │
│  │ LAYER 3: LLM-as-Judge Residue (expensive)        │   │
│  │  - Full context: issue + patch + L1/L2 results   │   │
│  │  - Structured assessment with confidence score   │   │
│  │  - Conservative: uncertain → ACCEPT              │   │
│  └────────────────────┬─────────────────────────────┘   │
│           ACCEPT or REJECT + specific feedback          │
└──────────────────────┬──────────────────────────────────┘
                       │
              ┌────────┴────────┐
              │                 │
           ACCEPT           REJECT
              │                 │
       Submit patch      Feed rejection reason
       to SWE-bench      back to agent for retry
       evaluation        (up to N retries)
```

### 2.1 Retry Protocol

- Maximum retries: 2 (so up to 3 total attempts per instance)
- On rejection, agent receives structured feedback:
  - Layer 1 rejection: specific structural issue (e.g., "SyntaxError on line 42 of models.py")
  - Layer 2 rejection: failed checker descriptions (e.g., "Patch does not modify the function mentioned in the traceback")
  - Layer 3 rejection: LLM's assessment summary
- If all 3 attempts are rejected, submit the last attempt anyway (don't waste the instance)

---

## 3. Model Selection

### 3.1 Coding Agent (mini-SWE-agent brain)

| Option | Cost | Expected Baseline | Notes |
|--------|------|-------------------|-------|
| **Qwen2.5-Coder-32B-Instruct** | Free (university GPU / Together AI free tier) | ~35-45% on subset | Best open-source option, fits on single A100 |
| **DeepSeek-Coder-V2-Lite** | Free (local) or ~$0.01/instance | ~30-40% | Lighter, faster iteration |
| **GPT-4o-mini** | ~$0.02-0.05/instance | ~40-50% | Cheap proprietary fallback |

**Recommendation:** Start with Qwen2.5-Coder-32B. Lower baseline (~35-45%) gives more room to demonstrate improvement from EvoEval. Total cost for 50 instances × 3 attempts = ~$0-15.

### 3.2 Checker Generator (Layer 2)

| Option | Cost | Notes |
|--------|------|-------|
| **GPT-4o-mini** | ~$0.01/instance | Good at structured code generation |
| **Qwen2.5-Coder-7B-Instruct** | Free (local) | Can generate Python scripts; lower quality |
| **Claude 3.5 Haiku** | ~$0.01/instance | Strong at following structured templates |

**Recommendation:** GPT-4o-mini. One call per instance to generate checkers. Total cost: 50 × $0.01 = $0.50.

### 3.3 LLM-as-Judge (Layer 3)

| Option | Cost | Notes |
|--------|------|-------|
| **GPT-4o-mini** | ~$0.02-0.05/instance | Sufficient for structured assessment |
| **Qwen2.5-72B-Instruct** | Free (Together AI) | Open-source alternative |

**Recommendation:** GPT-4o-mini. Only triggered for ~30-40% of instances. Total cost: ~20 instances × $0.03 = $0.60.

### 3.4 Meta-prompt Evolution (offline, one-time)

Use GPT-4o-mini for proposing meta-prompt mutations. ~20 LLM calls total across evolution. Cost: ~$0.50.

### Total Estimated Budget: $15-50

---

## 4. Dataset: 50-Instance SWE-bench Verified Subset

We use [50 instance SWE-bench Verified Subset](https://github.com/mariushobbhahn/SWEBench-verified-mini) as our benchmark.

---

## 5. Layer 1: Structural Sanity Filters

### 5.1 Checks to Implement

Each check is a Python function: `check_name(repo_path, patch_diff, issue_text) -> (bool, str)`
Returns (passed, reason_if_failed).

#### Check 1a: Syntax Validity
```python
def check_syntax(repo_path: str, patch_diff: str) -> tuple[bool, str]:
    """Apply patch, run py_compile on all modified .py files."""
    modified_files = extract_modified_files(patch_diff)
    for f in modified_files:
        if f.endswith('.py'):
            try:
                py_compile.compile(os.path.join(repo_path, f), doraise=True)
            except py_compile.PyCompileError as e:
                return False, f"SyntaxError in {f}: {e}"
    return True, ""
```

#### Check 1b: File Relevance
```python
def check_file_relevance(patch_diff: str, issue_text: str) -> tuple[bool, str]:
    """Check if patch modifies files mentioned/implied by the issue."""
    modified_files = extract_modified_files(patch_diff)
    # Extract file references from issue (tracebacks, explicit mentions)
    mentioned_files = extract_file_references(issue_text)
    if mentioned_files and not any(is_related(m, f) for m in mentioned_files for f in modified_files):
        return False, f"Patch modifies {modified_files} but issue references {mentioned_files}"
    return True, ""
```

#### Check 1c: Test File Contamination
```python
def check_no_test_modification(patch_diff: str) -> tuple[bool, str]:
    """Flag patches that modify test files (agent shouldn't know about tests)."""
    modified_files = extract_modified_files(patch_diff)
    test_files = [f for f in modified_files if 'test' in f.lower() or f.startswith('tests/')]
    if test_files:
        return False, f"Patch modifies test files: {test_files}"
    return True, ""
```

#### Check 1d: Import Integrity
```python
def check_imports(repo_path: str, patch_diff: str) -> tuple[bool, str]:
    """After applying patch, verify all imports in modified files resolve."""
    modified_files = extract_modified_files(patch_diff)
    for f in modified_files:
        if f.endswith('.py'):
            # Use ast to extract imports, verify they exist
            imports = extract_imports(os.path.join(repo_path, f))
            for imp in imports:
                if not can_resolve_import(imp, repo_path):
                    return False, f"Unresolved import '{imp}' in {f}"
    return True, ""
```

#### Check 1e: Scope Proportionality
```python
def check_scope(patch_diff: str, issue_text: str) -> tuple[bool, str]:
    """Flag patches with suspicious size relative to issue description."""
    added, deleted = count_diff_lines(patch_diff)
    num_files = len(extract_modified_files(patch_diff))
    # Heuristic thresholds (can be evolved)
    if num_files > 10:
        return False, f"Patch modifies {num_files} files — suspiciously broad"
    if added + deleted > 500:
        return False, f"Patch changes {added+deleted} lines — suspiciously large"
    if added + deleted == 0:
        return False, "Empty patch"
    return True, ""
```

#### Check 1f: Deletion-Without-Replacement
```python
def check_not_just_deletion(patch_diff: str) -> tuple[bool, str]:
    """Flag patches that only delete code without adding replacement logic."""
    added, deleted = count_diff_lines(patch_diff)
    if deleted > 10 and added == 0:
        return False, f"Patch deletes {deleted} lines without adding any code"
    if deleted > 20 and added < deleted * 0.2:
        return False, f"Patch deletes {deleted} lines but only adds {added}"
    return True, ""
```

### 5.2 Layer 1 Decision Logic

```python
def layer1_evaluate(repo_path, patch_diff, issue_text) -> tuple[str, str]:
    """Returns ('pass', '') or ('fail', reason)."""
    checks = [
        check_syntax(repo_path, patch_diff),
        check_file_relevance(patch_diff, issue_text),
        check_no_test_modification(patch_diff),
        check_imports(repo_path, patch_diff),
        check_scope(patch_diff, issue_text),
        check_not_just_deletion(patch_diff),
    ]
    for passed, reason in checks:
        if not passed:
            return 'fail', reason
    return 'pass', ''
```

---

## 6. Layer 2: Instance-Specific Generated Checkers

### 6.1 Meta-Prompt (the thing that evolves)

This is the prompt template sent to the checker-generator LLM. It gets evolved on the 15-instance evolution set.

**Seed meta-prompt v0:**

```
You are a code review expert. Given a GitHub issue and relevant source code,
generate 3-5 executable Python checker functions that verify whether a proposed
patch correctly addresses the issue.

## GitHub Issue
{issue_description}

## Relevant Source Files (pre-patch)
{relevant_source_snippets}

## Instructions
For each checker, write a Python function with this signature:
    def check_<name>(repo_path: str) -> tuple[bool, str]:
        """<description of what this checks>"""
        # ... implementation ...
        return (True/False, "reason if failed")

Available utility functions (already imported):
- read_file(repo_path, relative_path) -> str
- extract_function(source_code, function_name) -> str
- extract_class(source_code, class_name) -> str
- get_patch_diff(repo_path) -> str
- file_exists(repo_path, relative_path) -> bool
- count_occurrences(source_code, pattern) -> int

Focus your checkers on:
1. Does the patch modify the correct file(s) and function(s)?
2. Does the patch address the specific bug/feature described?
3. Does the patch handle the edge case(s) mentioned in the issue?
4. Does the patch preserve existing behavior for normal inputs?
5. Does the patch follow the codebase's patterns (error handling, naming, etc.)?

Output ONLY the Python functions, no explanation.
```

### 6.2 Checker Generation Pipeline

```python
def generate_checkers(issue_text: str, repo_path: str, meta_prompt: str) -> list[Callable]:
    """Generate instance-specific checkers using LLM."""

    # Step 1: Identify relevant source files
    relevant_files = identify_relevant_files(issue_text, repo_path)
    source_snippets = ""
    for f in relevant_files[:5]:  # Limit context
        content = read_file(repo_path, f)
        if len(content) > 3000:
            content = content[:3000] + "\n... (truncated)"
        source_snippets += f"\n### {f}\n```python\n{content}\n```\n"

    # Step 2: Generate checkers via LLM
    prompt = meta_prompt.format(
        issue_description=issue_text,
        relevant_source_snippets=source_snippets
    )
    response = call_llm(prompt, model="gpt-4o-mini", temperature=0.3)

    # Step 3: Parse and validate generated checker code
    checkers = parse_checker_functions(response)

    # Step 4: Dry-run each checker to verify it doesn't crash
    valid_checkers = []
    for checker in checkers:
        try:
            result = checker(repo_path)
            if isinstance(result, tuple) and len(result) == 2:
                valid_checkers.append(checker)
        except Exception:
            pass  # Discard checkers that crash

    return valid_checkers
```

### 6.3 Layer 2 Decision Logic

```python
def layer2_evaluate(repo_path, issue_text, meta_prompt) -> tuple[str, str, dict]:
    """
    Returns:
        decision: 'pass' | 'fail' | 'inconclusive'
        reason: explanation string
        details: {checker_name: (passed, reason)} for Layer 3 context
    """
    checkers = generate_checkers(issue_text, repo_path, meta_prompt)

    if len(checkers) == 0:
        return 'inconclusive', 'Could not generate valid checkers', {}

    results = {}
    for checker in checkers:
        try:
            passed, reason = checker(repo_path)
            results[checker.__name__] = (passed, reason)
        except Exception as e:
            results[checker.__name__] = (None, f"Checker error: {e}")

    # Decision logic
    valid_results = {k: v for k, v in results.items() if v[0] is not None}
    if len(valid_results) == 0:
        return 'inconclusive', 'All checkers errored', results

    pass_rate = sum(1 for v in valid_results.values() if v[0]) / len(valid_results)

    if pass_rate >= 0.8:
        return 'pass', f'{pass_rate:.0%} of checkers passed', results
    elif pass_rate <= 0.3:
        failed = [f"{k}: {v[1]}" for k, v in valid_results.items() if not v[0]]
        return 'fail', f'Only {pass_rate:.0%} passed. Failures:\n' + '\n'.join(failed), results
    else:
        return 'inconclusive', f'{pass_rate:.0%} of checkers passed (mixed)', results
```

---

## 7. Layer 3: LLM-as-Judge Residue

### 7.1 Judge Prompt

```
You are evaluating whether a code patch correctly resolves a GitHub issue.

## Issue Description
{issue_description}

## Proposed Patch
{patch_diff}

## Automated Analysis Results
Layer 1 (structural): PASSED
Layer 2 (instance-specific checkers):
{layer2_details}

## Task
Based on all the information above:
1. Does this patch correctly address the issue described? (yes/no/uncertain)
2. Could this patch introduce regressions? (yes/no/uncertain)
3. Confidence level: high / medium / low
4. If rejecting, provide specific feedback for the developer.

Respond in this exact JSON format:
{
    "verdict": "accept" | "reject" | "uncertain",
    "confidence": "high" | "medium" | "low",
    "reasoning": "...",
    "feedback_if_rejected": "..."
}
```

### 7.2 Layer 3 Decision Logic

```python
def layer3_evaluate(issue_text, patch_diff, layer2_details) -> tuple[str, str]:
    """Returns ('accept', '') or ('reject', feedback)."""
    prompt = JUDGE_PROMPT.format(
        issue_description=issue_text,
        patch_diff=patch_diff,
        layer2_details=format_layer2_results(layer2_details)
    )
    response = call_llm(prompt, model="gpt-4o-mini", temperature=0.1)
    judgment = parse_json(response)

    # Conservative policy: only reject on high-confidence rejections
    if judgment['verdict'] == 'reject' and judgment['confidence'] == 'high':
        return 'reject', judgment.get('feedback_if_rejected', 'Patch appears incorrect')
    else:
        # Accept if uncertain or low confidence — don't waste retries
        return 'accept', ''
```

---

## 8. Meta-Prompt Evolution (Offline Phase)

### 8.1 Evolution Protocol

Run on the 15-instance evolution set where ground truth (hidden tests) is accessible.

```
FOR generation = 1 to 5:
    FOR each instance in evolution_set (15 instances):
        1. Run mini-SWE-agent to generate patch
        2. Run Layer 2 with current meta-prompt to generate checkers
        3. Execute checkers on patch
        4. Compare checker predictions to ground truth:
           - Checker says fail + patch actually fails tests = TRUE NEGATIVE  (good)
           - Checker says pass + patch actually passes tests = TRUE POSITIVE (good)
           - Checker says fail + patch actually passes tests = FALSE NEGATIVE (bad — wasted retry)
           - Checker says pass + patch actually fails tests = FALSE POSITIVE (bad — missed bad patch)

    Compute fitness:
        fitness = (true_positives + true_negatives) / total
                  - 2.0 * false_negatives / total   # Heavy penalty for rejecting good patches
                  - 1.0 * false_positives / total    # Moderate penalty for accepting bad patches

    Use LLM to propose 3 meta-prompt mutations:
        "Here is the current meta-prompt: {current_prompt}
         Here are cases where the generated checkers were wrong:
         {failure_analysis}
         Propose a revised meta-prompt that would generate
         better checkers for these cases."

    Test each mutation on evolution set
    Keep the best-performing variant
    Discard the rest
```

### 8.2 What Evolves vs. What Stays Fixed

| Component | Evolves? | How |
|-----------|----------|-----|
| Layer 1 checks | Thresholds only | Tune scope/deletion thresholds on evolution set |
| Layer 2 meta-prompt | YES | Full evolutionary loop (Section 8.1) |
| Layer 2 checker template library | YES | Accumulate successful checker patterns |
| Layer 2 utility functions | NO | Fixed API (read_file, extract_function, etc.) |
| Layer 3 judge prompt | Lightly | Adjust based on common failure patterns |
| Retry budget / decision thresholds | YES | Tune pass_rate cutoffs on evolution set |

---

## 9. Evaluation Protocol

### 9.1 Baselines

| Configuration | Description |
|---------------|-------------|
| **B0: Agent-only** | mini-SWE-agent with pass@1 (single attempt, no verification) |
| **B1: Agent + 3 retries** | mini-SWE-agent with pass@3 (3 attempts, no verification, random retry) |
| **B2: Agent + static Layer 1** | Agent + Layer 1 structural checks only, reject+retry on failure |
| **B3: Agent + seed Layer 2** | Agent + Layer 1 + Layer 2 with seed meta-prompt (no evolution) |
| **E1: Agent + evolved Layer 2** | Agent + Layer 1 + Layer 2 with evolved meta-prompt |
| **E2: Agent + full EvoEval** | Agent + Layer 1 + evolved Layer 2 + Layer 3 |

### 9.2 Metrics

| Metric | Definition |
|--------|------------|
| **Resolve rate (pass@1)** | % of instances where first submitted patch passes hidden tests |
| **Resolve rate (pass@3 with EvoEval)** | % resolved within 3 attempts using EvoEval feedback |
| **Resolve rate (pass@3 random)** | % resolved within 3 random retries (no feedback) |
| **True rejection rate** | % of rejected patches that actually would have failed tests |
| **False rejection rate** | % of rejected patches that actually would have passed tests |
| **Layer distribution** | % of decisions made at each layer (L1 / L2 / L3) |
| **Cost per instance** | Average $ spent on LLM calls per instance |
| **Checker quality** | Precision/recall of Layer 2 checkers against ground truth |
| **Evolution gain** | Δ between seed meta-prompt and evolved meta-prompt performance |

### 9.3 Evaluation Procedure on Held-Out Set (35 instances)

```
FOR each instance in evaluation_set:
    # Attempt 1
    patch_1 = run_mini_swe_agent(instance)
    decision_1, feedback_1 = evoeval_evaluate(patch_1, instance)

    IF decision_1 == 'accept':
        submit(patch_1)  # Record result
    ELSE:
        # Attempt 2 (with feedback)
        patch_2 = run_mini_swe_agent(instance, prior_feedback=feedback_1)
        decision_2, feedback_2 = evoeval_evaluate(patch_2, instance)

        IF decision_2 == 'accept':
            submit(patch_2)
        ELSE:
            # Attempt 3 (with accumulated feedback)
            patch_3 = run_mini_swe_agent(instance, prior_feedback=feedback_1+feedback_2)
            submit(patch_3)  # Submit regardless

    # After all experiments: check against ground truth tests
    # Record: which attempt was submitted, did it pass, what did each layer say
```

---

## 10. Implementation TODOs

### Phase 1: Infrastructure

- [ ] **P1-1.** Fork mini-SWE-agent, verify it runs on a single SWE-bench instance locally
- [ ] **P1-2.** Write `select_instances.py`: select 50 instances from SWE-bench Verified stratified by repo and difficulty. Output `instance_ids.json` and `split.json`
- [ ] **P1-3.** Set up Docker infrastructure for SWE-bench evaluation (one instance at a time is fine)
- [ ] **P1-4.** Write `llm_client.py`: unified client supporting OpenAI API (GPT-4o-mini), Together AI / vLLM (Qwen2.5-Coder), with cost tracking
- [ ] **P1-5.** Write `diff_parser.py`: extract modified files, count added/deleted lines, identify modified functions from a git diff string
- [ ] **P1-6.** Run baseline B0: mini-SWE-agent + Qwen2.5-Coder-32B on all 50 instances, record pass@1. This is the number you need to beat
- [ ] **P1-7.** Run baseline B1: same agent, 3 random retries per instance, record pass@3. This is the ceiling for what retry-based approaches can achieve
- [ ] **P1-8.** Manually examine 10 failed patches from B0. Categorize failure modes. This informs Layer 2 checker design

**Deliverable:** Baseline numbers (B0, B1) + failure mode analysis document.

### Phase 2: Layer 1 Implementation

- [ ] **P2-1.** Implement all 6 Layer 1 checks as individual Python modules
- [ ] **P2-2.** Write `layer1/__init__.py` aggregation function
- [ ] **P2-3.** Test Layer 1 on the 50 baseline patches from P1-6: how many would it reject? How many rejections are correct (patch actually fails tests)?
- [ ] **P2-4.** Tune thresholds: adjust scope/deletion limits to minimize false rejections
- [ ] **P2-5.** Run baseline B2: agent + Layer 1 + retry on rejection. Record resolve rate

**Deliverable:** Layer 1 module + B2 numbers + false rejection analysis.

### Phase 3: Layer 2 Implementation

- [ ] **P3-1.** Write `utility_functions.py`: implement read_file, extract_function, extract_class, get_patch_diff, file_exists, count_occurrences. These must be robust (handle missing files, encoding issues, etc.)
- [ ] **P3-2.** Write seed meta-prompt v0 (see Section 6.1)
- [ ] **P3-3.** Write `checker_generator.py`: takes issue + repo + meta-prompt, returns list of executable checker functions
- [ ] **P3-4.** Write `checker_executor.py`: sandboxed execution of generated checkers with timeout (5 sec per checker) and error handling
- [ ] **P3-5.** Test checker generation on 5 evolution-set instances manually. Examine generated checkers for quality. Iterate on meta-prompt v0 by hand if needed before starting evolution
- [ ] **P3-6.** Write `layer2/__init__.py` decision logic with pass_rate thresholds
- [ ] **P3-7.** Run baseline B3: agent + Layer 1 + seed Layer 2 + retry. Record resolve rate

**Deliverable:** Layer 2 module + B3 numbers + example generated checkers for 5 instances.

### Phase 4: Evolution Loop

- [ ] **P4-1.** Write `fitness.py`: given meta-prompt + evolution set, compute fitness score (Section 8.1 formula)
- [ ] **P4-2.** Write `mutation_proposer.py`: sends current meta-prompt + failure cases to LLM, gets back 3 proposed mutations
- [ ] **P4-3.** Write `evolve_meta_prompt.py`: orchestrates the full evolution loop (5 generations × 3 mutations × 15 instances)
- [ ] **P4-4.** Run evolution. Log each generation's fitness + winning meta-prompt variant
- [ ] **P4-5.** Analyze evolution trajectory: does fitness improve? Does it plateau? Which mutations helped?
- [ ] **P4-6.** Save best evolved meta-prompt as `meta_prompt_evolved.txt`
- [ ] **P4-7.** Run E1: agent + Layer 1 + evolved Layer 2 + retry on the 35 evaluation instances. Record resolve rate

**Deliverable:** Evolution trajectory plot + evolved meta-prompt + E1 numbers.

### Phase 5: Layer 3 + Full System

- [ ] **P5-1.** Write `llm_judge.py` with the structured judge prompt (Section 7.1)
- [ ] **P5-2.** Write `orchestrator.py`: full L1→L2→L3 pipeline with retry logic
- [ ] **P5-3.** Run E2: full EvoEval (L1 + evolved L2 + L3) on 35 evaluation instances. Record resolve rate
- [ ] **P5-4.** Run cost analysis: total LLM spend per configuration

**Deliverable:** Full system + E2 numbers + cost breakdown.

### Phase 6: Analysis & Paper

- [ ] **P6-1.** Generate comparison table: B0 vs B1 vs B2 vs B3 vs E1 vs E2
- [ ] **P6-2.** Per-layer analysis: what fraction of decisions were made at L1 / L2 / L3?
- [ ] **P6-3.** False rejection analysis: how many correct patches did EvoEval incorrectly reject?
- [ ] **P6-4.** Evolution ablation: seed meta-prompt vs evolved meta-prompt performance
- [ ] **P6-5.** Qualitative case studies: 3-5 instances where EvoEval caught a bad patch and feedback led to a correct retry
- [ ] **P6-6.** Failure case studies: 2-3 instances where EvoEval failed (missed a bad patch or rejected a good one)
- [ ] **P6-7.** Write paper (target: ASE 2026 NIER or FormaliSE 2026)

**Deliverable:** Paper draft.

---

## 11. Risk Mitigation

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Baseline is already >70% (too high to show improvement) | Low (using open-source model) | If baseline too high, switch to weaker model or use SWE-bench Pro subset |
| Generated checkers always crash or produce garbage | Medium | Invest in robust utility functions (P3-1) + sandboxed execution (P3-4). If persistent, fall back to simpler template-based checkers |
| Evolution doesn't improve over seed meta-prompt | Medium | This is a publishable negative result. Report it honestly. Focus paper on Layer 1+2 analysis and the baseline improvement from non-evolved checkers |
| False rejections waste retries and hurt performance | Medium | Conservative decision thresholds (accept when uncertain). Measure false rejection rate explicitly |
| LLM API costs exceed budget | Low | GPT-4o-mini is very cheap. Total estimated: $15-50. Monitor with cost tracking in llm_client.py |
| SWE-bench Docker setup issues | High | Start P1-1 immediately. Budget extra time. Use official SWE-bench Docker images |

---

## 12. Key References

| Paper | Relevance |
|-------|-----------|
| Live-SWE-agent (Xia et al., Nov 2025) | Self-evolving agent on SWE-bench; closest comparison point |
| Agent-as-a-Judge (Zhuge et al., ICML 2025) | Agentic evaluation framework; our work extends this with generated executable checkers |
| A3-python (Young & Bjørner, RiSE MSR, Feb 2026) | AI-generated verification cascades; inspiration for our kitchen-sink architecture |
| CASTLE (Dubniczky et al., TASE 2025) | Tool combination for vulnerability detection; validates multi-method approach |
| AlphaEvolve (Novikov et al., May 2025) | Evolutionary code optimization; inspiration for meta-prompt evolution |
| SWE-bench Verified (OpenAI, 2024) | Benchmark + evaluation methodology |
| SWE-bench Pro (Scale AI, Sep 2025) | Harder benchmark; potential extension target |
| CodeJudgeBench (Jiang et al., 2025) | LLM-as-judge limitations for code; motivates our checker-based approach over pure LLM judging |

---

## 13. Success Criteria

| Criterion | Minimum | Stretch |
|-----------|---------|---------|
| Resolve rate improvement (E2 vs B0) | +2 percentage points | +5 percentage points |
| Resolve rate improvement (E2 vs B1) | +1 percentage point | +3 percentage points |
| Evolution gain (E1 vs B3) | Any positive Δ | +2 percentage points |
| True rejection rate | >60% | >80% |
| False rejection rate | <30% | <15% |
| Paper submission | Workshop paper (4 pages) | Full research paper |

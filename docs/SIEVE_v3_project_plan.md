# SIEVE v3: Self-Improving Evaluation via Verification Evolution for SWE Agents

## Project Plan — Revised for Reproduction Test + LLM-as-a-Judge Layers

---

## 1. Status Update

Work completed under v2:

- **Layer 1 — Static analysis** (AST parse, flake8 delta, Semgrep rules): done
- **Layer 2a — Regression tests** using SWE-bench Verified P2P tests: done
- Baseline mini-SWE-agent runs on the pilot instance set: done

Remaining components, covered by this plan:

- **Layer 2b — Reproduction test generation and verdict** (this was underspecified in v2)
- **Layer 3 — LLM-as-a-Judge tiebreaker** on ambiguous reproduction verdicts
- **Self-evolution protocol** for prompts, rubrics, and failure-pattern memory
- **Integration into the mini-swe-agent retry loop** with structured feedback
- **Evaluation and ablation runs**

This document replaces §3.2–§3.6 and §6 of the v2 plan and adds a new §5 (self-evolution) and §6 (research prompts). Sections on motivation, landscape, and overall experimental design are preserved from v2 with minor updates.

---

## 2. Design Principles

Four principles anchor the v3 design and explain why it differs from v2's description:

1. **The reproduction test system is a filter, not a ranker.** Mini-swe-agent produces one candidate patch per attempt. The job is to decide PASS / FAIL / UNCERTAIN with useful feedback on FAIL, not to discriminate among N candidates.

2. **Reproduction test generation must be patch-blind.** If the generator sees the candidate patch, the test will drift toward confirming whatever the patch does — including wrong things. The patch enters the pipeline exactly once, at verdict time.

3. **The reproduction artifact is amortized across retries.** Test generation is a function of (issue, repo), not (issue, repo, patch). So it runs once per issue and is cached; each mini-swe-agent retry only incurs the cost of re-executing the cached tests against the new patch.

4. **The judge is a tiebreaker, not a primary gate.** R2E-Gym's hybrid verifier lifts Best@26 from 43.7% to 51.0% by using LLM score as a tiebreaker among execution-quality candidates, not as a parallel voter. The judge fires only on UNCERTAIN reproduction verdicts (~15–25% of patches), and never overrides an ACCEPT verdict from tests.

---

## 3. Reproduction Test System

### 3.1 Two-Phase Architecture

The pipeline splits along the information boundary of "has the candidate patch been seen yet." Phase A runs once per issue; Phase B runs once per candidate patch.

```
Phase A — patch-blind (cached per issue)
    │
    │  Stage A1: Issue prep + localization
    │  Stage A2: Small-mask generation (3 masks × 2 samples = 6 candidates)
    │  Stage A3: Buggy-repo gate
    ▼
Gated reproduction tests (tagged by bucket)
    │
    │  Candidate patch enters here
    ▼
Phase B — patch-aware
    │
    │  Stage B1: Run tests on patched repo
    │  Stage B2: Weighted vote → PASS / FAIL / UNCERTAIN
    │  Stage B3: Feedback synthesis (only on FAIL)
    ▼
Verdict + optional feedback
```

### 3.2 Phase A — Patch-Blind Generation

**Stage A1 — issue preparation and localization.** Parse the issue into structured fields: body, extracted traceback (if present), reporter-provided reproducer snippet (if present), expected/actual pair (regex-extracted or LLM-extracted). Run Otter-style two-call localization (test-file localization → test-function + focal-file localization) with minimum-edit-distance repair on hallucinated filenames. Gemini Flash or Haiku-class for both calls. Output: structured issue object + localized focal file paths.

**Stage A2 — small-mask generation.** Three prompt masks, two samples per mask at temperature 0.5, six candidates total. The masks vary *what the model sees*, not how it samples:

| Mask | Input to generator | Rationale |
|------|-------------------|-----------|
| Raw issue | Issue title + body only | Agentless baseline |
| Traceback-first | Issue + extracted traceback only | Biases toward exception-type assertions |
| Snippet-first | Issue + reporter's reproducer snippet (falls back to raw if absent) | Uses reporter's own failure scenario |

The literature is unusually consistent that structural diversity beats temperature diversity for reproduction test generation (e-Otter++, Otter++, Issue2Test). Three masks is the smallest configuration that preserves this gain while keeping total generation under ~6 Sonnet-class calls.

Every candidate is a standalone pytest-compatible script emitting:
- Agentless text markers: `Issue reproduced` / `Issue resolved` / `Other issues` (printed to stdout)
- CodeMonkeys exit codes: 2 = bug present, 0 = fixed
- No repo-specific fixtures (only stdlib + pytest)

Dual signaling is cheap and makes downstream classification robust to either failure mode.

**Stage A3 — buggy-repo gate.** Run each candidate on the unpatched repo. Drop any whose failure is `ImportError`, `SyntaxError`, `NameError`, or a fixture error — none of these constitute a real reproduction. Tag survivors by failure bucket:

- Bucket A (weight 1.0): assertion failure with a message that references the issue
- Bucket B (weight 0.7): specific exception match (the exact exception type named in the issue)
- Bucket C (weight 0.4): generic error consistent with the issue

Keep all survivors. If zero tests survive, emit UNCERTAIN immediately and skip Phase B — the pipeline has no signal. Caveat this for mini-swe-agent: UNCERTAIN from zero-signal is meaningfully different from UNCERTAIN from test disagreement, so tag it in the output.

**Caching.** The Phase A output is `{issue_id → {gated_tests: [...], buckets: {...}, rubric_hint: {...}}}`, serialized to disk. Mini-swe-agent retries re-use this artifact; regenerating is only triggered if the issue object changes.

### 3.3 Phase B — Patch-Aware Verdict

**Stage B1 — run tests on patched repo.** Apply the candidate patch, execute all gated tests, collect `{test_id, exit_code, stdout, stderr, pytest_outcome}`. No refinement, no re-generation — the tests are frozen artifacts from Phase A.

**Stage B2 — weighted vote.** Compute `S = Σ(wᵢ · passᵢ) / Σwᵢ`, where `passᵢ ∈ {0, 1}` indicates whether test i passed on the patched repo.

- **PASS** if `S ≥ 0.70`: patch plausibly fixes the issue
- **FAIL** if `S ≤ 0.40`: patch does not fix the issue
- **UNCERTAIN** if `0.40 < S < 0.70`: tests disagree

These thresholds are deliberately asymmetric. False PASS is the most expensive error for the filter (the patch gets shipped), so we err on the side of UNCERTAIN. Thresholds are a tuning hyperparameter — calibrate on a validation slice in §5.

**Stage B3 — feedback synthesis (FAIL only).** Pick the highest-bucket failing test. Extract assertion message + minimal traceback frames. Prompt a Haiku-class model to produce a scenario description in natural language — *not* the test code. The critical constraint: mini-swe-agent should receive "your patch still produces None when the input contains a trailing slash — the issue expects canonicalization" rather than the test itself. Showing the test causes the agent to overfit to that specific case rather than addressing the underlying bug.

### 3.4 Integration with Mini-SWE-Agent Retry Loop

```
for attempt in range(max_retries + 1):
    patch = mini_swe_agent(issue, prior_feedback)

    # Layer 1 (done): static analysis
    if static_check(patch).failed:
        prior_feedback = static_feedback(patch); continue

    # Layer 2a (done): regression
    if regression_check(patch).failed:
        prior_feedback = regression_feedback(patch); continue

    # Layer 2b (new): reproduction
    repro_result = phase_B(patch, phase_A_cache[issue])
    if repro_result.verdict == "FAIL":
        prior_feedback = repro_result.feedback; continue
    if repro_result.verdict == "PASS":
        return patch  # accept

    # Layer 3 (new): judge tiebreaker, only on UNCERTAIN
    judge_result = judge(issue, patch, repro_result)
    if judge_result.verdict == "ACCEPT":
        return patch
    if judge_result.verdict == "REJECT":
        prior_feedback = judge_feedback(patch, repro_result, judge_result); continue
    # judge UNCERTAIN: on last attempt accept conservatively, else retry
    if attempt == max_retries:
        return patch  # ship with low-confidence tag
    prior_feedback = uncertain_feedback(patch, repro_result, judge_result)
```

The Phase A cache is built once on first entry to Layer 2b. All subsequent retries hit only Phase B + judge (both ~$0 LLM cost for Phase B, ~$0.02 for judge).

---

## 4. LLM-as-a-Judge Tiebreaker

### 4.1 When It Fires

The judge fires only when the reproduction layer emits UNCERTAIN with non-zero signal (zero-signal UNCERTAIN skips the judge — there's nothing to tiebreak). Empirically this should be ~15–25% of patches. It never overrides a PASS or FAIL from the reproduction layer.

### 4.2 Inputs and Input Hygiene

The R2E-Gym attention finding — that execution-free judges attend to agent sentiment like "Great, this should fix it!" rather than the code — is the most actionable bias result in the literature. The input specification is primarily about exclusion:

**Include:**
- Issue text (title + body + traceback), verbatim
- Unified diff of the candidate patch
- ±30 lines of pre-patch code around each hunk
- For each *failing* reproduction test: full test code + assertion message + minimal traceback
- For each *passing* reproduction test: test name + one-line summary only

**Strip:**
- Any agent trajectory, chain-of-thought, or self-rationalization
- Patch-level comments that describe the patch ("Fix for issue #1234 — corrects None handling")
- Author or provenance metadata

**Exclude (deliberately):**
- Static analysis output and regression test results — already consumed by earlier layers
- The full passing-test code — giving it to the judge biases toward ACCEPT without making the judge reason about failure legitimacy

### 4.3 Rubric (Fixed, 4 Items)

Atomic binary rubric items, verb-first phrasing following Agentic Rubrics conventions. Weights drawn from the failure-utility analysis (Root Cause Missed = 17.5% of rubric catches, Scope Creep = 2.6%, etc.).

| # | Item | Weight |
|---|------|--------|
| 1 | Modifies code in a region that plausibly addresses the root cause described in the issue | 3 |
| 2 | Addresses the reported behavior rather than only suppressing its visible symptom (no hardcoded values for reported inputs, no broad `except:` that silently swallows, no early-return guards on the specific reported case) | 3 |
| 3 | Makes only changes necessary for the issue, without broad refactors, unrelated API changes, or test weakening | 2 |
| 4 | For the currently-failing reproduction tests, the majority are legitimate reproductions rather than brittle/flaky | 3 |

Item 4 is the core tiebreaker signal. Items 1–3 are the integrity floor that catches test-overfitting hacks, wrong-cause fixes, and scope creep — the three pathologies the execution layers structurally cannot see.

### 4.4 Output Schema (Gemini JSON Mode)

```json
{
  "rubric_scores": {
    "root_cause": 0 | 1,
    "not_symptom_only": 0 | 1,
    "no_scope_creep": 0 | 1,
    "failing_tests_legitimate": 0 | 1
  },
  "failing_test_assessments": [
    {"test_name": "string", "legitimate": 0 | 1, "reason": "≤ 20 words"}
  ],
  "reasoning": "≤ 5 sentences",
  "verdict": "ACCEPT" | "REJECT" | "UNCERTAIN",
  "confidence": "low" | "medium" | "high"
}
```

The short `reasoning` string is deliberate: CodeJudge found explicit CoT *hurt* accuracy on code, but constrained reasoning inside structured output consistently helps. Per-test assessments give observability for calibration.

### 4.5 Aggregation Rule

Weighted rubric score `S = Σ(wᵢ · sᵢ) / Σwᵢ`, then lexicographic decision:

- **Upgrade UNCERTAIN → PASS** only if: `verdict == ACCEPT` AND `confidence == high` AND `S ≥ 0.75` AND `not_symptom_only == 1`
- **Downgrade UNCERTAIN → FAIL** if: `verdict == REJECT` OR `root_cause == 0` OR `not_symptom_only == 0`
- **Stay UNCERTAIN** otherwise

Items 1 and 2 act as hard-fail gates — no amount of high-confidence ACCEPT can override "patch isn't in the right place" or "patch only suppresses the symptom." This directly encodes the Team Atlanta failure taxonomy (Right Root Cause, Wrong Patches; functionality-altered via exception swallowing) into the aggregation logic rather than leaving it for the model to reason about.

### 4.6 Bias Mitigations

| Bias | Mechanism |
|------|-----------|
| Self-preference | Use Gemini Flash specifically (measured ~20% self-preference vs ~33% baseline in Google DX study) |
| Agent-sentiment contamination | Strip all agent narrative and patch-level comments |
| Position bias | N/A — pointwise, not pairwise |
| Length/verbosity | Binary atomic items; anonymize patch |
| Sycophancy/anchoring | Neutral verb-first rubric phrasing; avoid "does this patch correctly fix..." |

Optional: N=3 self-consistency voting on the verdict field only, activated if calibration shows flakiness > 15%.

---

## 5. Self-Evolution Protocol

### 5.1 Evolvable Artifacts

Three artifacts evolve over the project:

1. **Reproduction mask prompts** (three fixed prompts for masks 1–3, plus scoring templates)
2. **Judge rubric** (items, weights, aggregation thresholds)
3. **Failure-pattern library** (shared between both layers, described in §5.3)

Two non-LLM artifacts carried over from v2 also evolve:

4. Semgrep rules (from v2)
5. Feedback-synthesis prompt (shared between reproduction FAIL and judge REJECT)

### 5.2 Evolution Cycle

One calibration cycle, repeated up to twice in the remaining timeline:

**Step 1 — Validation slice.** Label 50 patches from SWE-bench Verified with ground-truth correctness (using the hidden test suite). Stratify: 15 correct, 15 test-passing-but-wrong, 10 test-failing-genuinely-wrong, 10 edge cases where the hidden tests themselves are noisy. This slice is hold-out from the main evaluation set.

**Step 2 — Error classification.** Run SIEVE-v0 (the post-integration baseline) on the slice. For each patch, record the verdict at each layer. Classify errors:

- *False PASS*: SIEVE accepted a patch the hidden tests reject. Root cause in which layer?
- *False FAIL*: SIEVE rejected a patch the hidden tests accept. Root cause in which layer?
- *UNCERTAIN leakage*: patches where the final verdict was UNCERTAIN and the judge couldn't resolve it. What rubric item would have helped?

**Step 3 — Artifact edits.** For each error class, propose targeted edits:

| Error pattern | Evolvable artifact | Edit type |
|---|---|---|
| Repro test missed a failure mode | Reproduction mask 2 (traceback-first) | Add instruction: "test the specific exception trajectory, including wrapper exceptions" |
| Patch hardcoded reported value, passed repro | Judge rubric item 2 | Add concrete negative example to the item description |
| Judge ACCEPT overrode a brittle passing test | Aggregation rule | Tighten threshold `S ≥ 0.75 → 0.80` |
| Feedback was too generic, retry didn't improve | Feedback synthesis prompt | Add: "cite the specific expected vs actual value from the failing test" |

**Step 4 — Validate on held-out.** Re-run SIEVE-v1 on 10 patches held out from the slice. The evolution counts as a gain only if held-out accuracy improves without regressing the original slice. If the held-out set regresses, revert.

### 5.3 Failure-Pattern Library (ExpeRepair-style Semantic Memory)

A small persistent memory indexed by issue embedding:

```python
failure_pattern = {
  "pattern_id": "symptom_suppression_none_return",
  "description": "Patch catches the exception and returns None instead of fixing cause",
  "example_issue": "django__django-12345",
  "example_patch_diff": "...",
  "detection_rule": "rubric_item_2 == 0 AND diff contains 'except.*: return None'",
  "recommended_feedback": "Your patch returns None on the error path, but the issue expects..."
}
```

At judge time, retrieve top-3 patterns by issue-embedding similarity and inject them into the rubric item 2 context as concrete negative examples. This is ExpeRepair's dual-memory mechanism adapted: retrieval-augmented rubric application rather than retrieval-augmented repair.

Capped at 20 patterns with ADD / REMOVE / UPDATE operations after each evolution cycle, following ExpeRepair's insight-bloat prevention.

### 5.4 Ablation-Driven Refinement

Four ablations to run on the validation slice before committing to SIEVE-v1:

| Ablation | What's being tested | Decision it drives |
|---|---|---|
| 2 masks vs 3 masks vs 4 masks (add AssertFlip) | Marginal value of each mask | Drop or add masks based on F→P delta |
| Weighted vs unweighted voting | Bucket weighting value | Keep weights or simplify to majority |
| Threshold sweep (0.40/0.70 → 0.35/0.75 → 0.45/0.65) | PASS/FAIL threshold asymmetry | Set final thresholds |
| Judge with vs without rubric item 4 | Is test-legitimacy assessment pulling weight? | Simplify rubric if item 4 is noisy |

An MIPROv2 / DSPy pass is an optional stretch goal — the Evidently case study reports a 64% → 96% accuracy lift from minutes of automated prompt optimization, which is the single most cost-effective improvement available. Include as a Week 3 stretch task if baseline calibration leaves gaps.

---

## 6. Research Prompts

The following prompts are designed to be pasted into a frontier model with web search enabled (Claude or Gemini with search, or a research-mode tool) to deepen specific design decisions. Each prompt is structured as a research task, not a question.

### 6.1 Enriching the Rubric with Additional Failure Taxonomies

> I am designing an LLM-as-a-Judge for SWE-bench patches. The judge fires as a tiebreaker on patches where reproduction tests partially pass and partially fail. My current rubric has 4 atomic binary items: (1) patch targets the root cause, (2) patch addresses behavior rather than suppressing symptoms, (3) no scope creep, (4) majority of failing reproduction tests are legitimate.
>
> Research task: Find published taxonomies of incorrect-but-test-passing patches published in 2024–2026 beyond PatchDiff (arXiv:2503.15223), Aleithan SWE-Bench+ (arXiv:2410.06992), and Team Atlanta. For each taxonomy, (a) identify failure modes not covered by my 4 items, (b) assess whether each uncovered mode is *detectable by inspection alone* (suitable for a judge) or *requires execution* (not suitable), and (c) recommend at most 2 additional rubric items that cover the largest uncovered inspection-detectable modes.
>
> Be specific: cite papers, quote relevant failure categories, and give concrete rubric item phrasings using verb-first format ("Avoids...", "Ensures...", "Modifies..."). Skip items that are redundant with my current 4.

### 6.2 Calibration Methods for Small Judge Models

> I am deploying Gemini 2.5 Flash as an LLM-as-a-Judge on SWE-bench patches. The judge emits binary per-rubric-item scores plus an overall verdict (ACCEPT / REJECT / UNCERTAIN) with verbal confidence (low / medium / high). I need to calibrate the verbal confidence so "high" actually means high reliability.
>
> Research task: Survey calibration methods published 2023–2026 for small/mid-size judge models (< 100B parameters). Focus on: (1) isotonic regression on 50–100 labeled examples — is this the right baseline or is there something better in that labeled-data budget? (2) logprob-based confidence extraction via Gemini API — what's possible and what's the accuracy gain over verbalized confidence? (3) self-consistency voting (N=3, N=5) — cost-accuracy tradeoff specifically for binary rubric items versus overall verdicts. (4) Any method that outperforms these with comparable labeled-data budget.
>
> Deliver: a ranked list of 3 calibration approaches I should try, with effort estimate (hours) and expected accuracy gain for each. Cite the papers and include any benchmark numbers relevant to judges.

### 6.3 Alternative Structural Masks for Reproduction Test Generation

> I am generating reproduction tests for SWE-bench issues using structural-diversity prompt masks (Otter++ / e-Otter++ style). Currently I use 3 masks: (A) raw issue, (B) issue plus extracted traceback, (C) issue plus reporter's reproducer snippet.
>
> Research task: Identify 2–4 additional structural masks from the 2024–2026 literature on reproduction test generation that are (1) known to produce semantically different tests than temperature sampling, (2) implementable with a single LLM call, and (3) likely to catch failure modes the 3 current masks miss. Candidates I am aware of: hypothesis decomposition (Issue2Test), AssertFlip (generate passing test then invert), paraphrase (Otter++).
>
> For each candidate mask, assess: empirical F→P gain reported in papers, implementation complexity, and whether the mask is redundant with my existing 3. Recommend 0–2 masks to add based on marginal-gain-per-call. Do not recommend adding all of them — defend what I should skip and why.

### 6.4 Patch Overfitting Detectors That Complement LLM-as-a-Judge

> My LLM-as-a-Judge has a rubric item for "patch addresses behavior rather than suppressing symptoms." It catches the obvious cases (hardcoded values, broad except:) but may miss subtler overfitting.
>
> Research task: Find work on mechanically detectable overfitting patterns in patches — specifically, static or lightweight-dynamic detectors that run without an LLM and catch patches that will pass generated tests but fail hidden tests. Candidates: differential testing (DiffTGen), mutation-based adequacy (FIXCHECK), scope-of-change metrics, static dataflow showing the patch only handles the exact input from the issue.
>
> For each detector, evaluate: precision on SWE-bench-style patches (if measured), false positive rate on correct minimal patches, implementation effort, whether it's complementary to or redundant with a 4-item rubric judge. Deliver: ranked list of at most 3 detectors worth adding to Layer 1 as pre-filters before the judge fires, with specific integration points.

### 6.5 Self-Evolution Mechanisms for Evaluation Criteria

> I am implementing a self-evolution protocol that updates (a) reproduction-test generation prompts, (b) judge rubric items and weights, and (c) a retrieval-augmented failure-pattern memory, based on a 50-patch validation slice with ground-truth labels. My current plan uses ExpeRepair-style dual memory with ADD / REMOVE / UPDATE operations, plus optional MIPROv2 prompt optimization.
>
> Research task: Survey self-evolving evaluator systems published 2024–2026. For each, identify: (1) what specifically evolves (prompts, rubrics, memory, thresholds, or model parameters), (2) the evolution signal (hidden labels, self-consistency, divergence, preference data), (3) the update mechanism (gradient, discrete edit, bayesian search, reinforcement), (4) evidence of generalization vs overfitting to the evolution set. Specifically assess: EvalGen / criteria drift (UIST 2024), Rubrics-as-Rewards, Recursive Rubric Decomposition (arXiv:2602.05125), SAFE, and any newer work.
>
> Deliver: 2 concrete mechanisms I should adopt that are compatible with a ~50-labeled-patch budget, with specific implementation steps and the expected risk of overfitting. Flag any mechanism that requires > 500 labeled examples as out of scope.

---

## 7. Experimental Design (Updated)

### 7.1 Configurations

| Configuration | Description |
|---|---|
| **Baseline** | Raw mini-SWE-agent, Pass@1 |
| **+Static** | Add Layer 1 only, retry on static failure (done) |
| **+Static+Regression** | Add Layers 1+2a, retry on static or regression failure (done) |
| **+SIEVE-v0-full** | Add Layer 2b (reproduction) and Layer 3 (judge), no evolution |
| **+SIEVE-v1-evolved** | After one evolution cycle on validation slice |
| **+SIEVE-v1-MIPRO** | Optional: MIPROv2-optimized judge prompt |

### 7.2 Metrics

**Primary:**
- Pass@1-with-retries (after up to K=2 SIEVE-guided retries), measured against SWE-bench Verified ground truth

**Diagnostic:**
- **Reproduction F→P rate**: fraction of generated tests that fail on buggy repo (Stage A3) and flip to pass on the gold patch
- **Per-layer catch rate**: fraction of incorrect patches uniquely caught by each layer (where "uniquely" = upstream layers would have accepted)
- **False PASS rate**: fraction of correct-by-SIEVE patches that hidden tests reject (most expensive error)
- **False FAIL rate**: fraction of rejected-by-SIEVE patches that hidden tests accept
- **Judge firing rate**: fraction of patches reaching UNCERTAIN at the reproduction layer
- **Judge accuracy on UNCERTAIN**: how often does the judge's verdict match the ground-truth outcome
- **Retry delta**: Pass@1 of retry-with-feedback minus Pass@1 of blind retry
- **Cost per instance**: total LLM dollars and wall-clock time

### 7.3 Instance Selection

100 instances from SWE-bench Verified total:
- **Main evaluation set**: 50 instances (same stratification as v2)
- **Validation slice for evolution**: 40 instances (50 labeled, 10 additional for evolution held-out)
- **Final held-out**: 10 instances not touched during any evolution cycle

The validation slice and held-out set do not overlap with the main evaluation set.

### 7.4 Ablations (Run on Validation Slice)

As detailed in §5.4: mask count, weighted voting, threshold sweep, judge item-4 contribution. Plus one new ablation:

- **Judge fires on UNCERTAIN only vs fires on all patches**: measure whether running the judge on PASS/FAIL verdicts catches additional errors at the cost of precision. Confirms or refutes the tiebreaker-only design.

---

## 8. Week-by-Week Plan (Remaining)

Assuming two weeks completed (static + regression). This plan covers the remaining three weeks with buffer.

### Week 1 of remainder: Reproduction Test System (Days 1–7)

**Days 1–2: Phase A implementation**
- Issue parser (extract traceback, snippet, expected/actual pair)
- Two-call localization with edit-distance repair
- Three mask prompt templates (raw / traceback-first / snippet-first)
- Candidate generator with dual-signaling (markers + exit codes)

**Days 3–4: Phase A validation**
- Run on 20 issues; measure: how many candidates survive the buggy-repo gate?
- Tune gate: if <3 survive on average, loosen bucket C; if >8 survive and most are bucket C, tighten
- Target: median 4 surviving tests per issue with ≥1 in bucket A

**Days 5–6: Phase B + feedback**
- Patch application + test execution harness
- Weighted vote + 3-state verdict
- Feedback synthesis prompt for FAIL cases
- Unit-test the verdict with synthetic good/bad patches

**Day 7: Integration test**
- Full Phase A + B on 10 issues with 2–3 patches each (mix of correct and incorrect)
- Verify: correct patches yield PASS, deliberately wrong patches yield FAIL, ambiguous patches yield UNCERTAIN
- Measure initial F→P rate, UNCERTAIN rate

**Deliverable:** Working reproduction test pipeline with initial quality metrics.

### Week 2 of remainder: Judge + Integration + Evolution (Days 8–14)

**Days 8–9: Judge implementation**
- Input assembly with stripping logic (agent narrative, comments, provenance)
- Fixed 4-item rubric prompt
- Gemini Flash structured output with `response_schema`
- Aggregation rule with the two hard-fail gates

**Days 10–11: Full cascade integration**
- Wire into mini-swe-agent retry loop (see §3.4)
- Phase A caching layer
- Aggregator: compose all four layers into single verdict + feedback
- End-to-end run on 20 main-set instances with K=2 retries

**Days 12–13: Evolution cycle 1**
- Label 50 validation-slice patches with ground-truth outcomes
- Error classification per §5.2
- Propose edits to rubric / mask prompts / feedback prompt
- Run SIEVE-v1 on validation slice held-out (10 instances); keep edits only if non-regressive
- Start building failure-pattern library (§5.3) from observed cases

**Day 14: Ablation day**
- Run the four core ablations (§5.4) on validation slice
- Record results, lock in hyperparameters for final runs

**Deliverable:** SIEVE-v1 with calibration complete, hyperparameters locked, failure-pattern library seeded.

### Week 3 of remainder: Full Evaluation + Report (Days 15–21)

**Days 15–16: Main evaluation runs**
- SIEVE-v0 and SIEVE-v1 on full 50-instance main set
- Record per-layer verdicts, feedback quality, retry outcomes
- Separately: baseline and +Static+Regression runs for comparison (re-use prior results if possible)

**Day 17: Held-out run**
- SIEVE-v1 on the final 10 held-out instances
- Check for overfitting to evolution slice

**Day 18: Optional MIPRO pass (stretch)**
- If time permits: run DSPy / MIPROv2 on the judge prompt using 30 validation patches
- Re-evaluate on held-out; include as SIEVE-v1-MIPRO if it helps

**Days 19–20: Analysis**
- Per-layer contribution tables
- Cost analysis (real dollars spent, per layer)
- Case studies: 3 where SIEVE caught something, 2 where it missed, 1 where evolution helped
- Ablation results write-up

**Day 21: Report draft**
- 8–12 page write-up following v2 structure
- Figures: cascade flow, per-layer contribution, evolution gains, cost breakdown

**Deliverable:** Complete report, clean GitHub repo, reproducible experiment scripts.

---

## 9. Budget (Revised)

| Item | Cost |
|------|------|
| Mini-swe-agent (GPT-5-mini) baseline + retries, 100 instances | ~$75 |
| Reproduction Phase A (Sonnet-class generation × 6 per issue × 100 issues) | ~$30 |
| Reproduction Phase A auxiliary (Haiku localization × 100 issues) | ~$2 |
| Reproduction Phase B (execution only, zero LLM) | $0 |
| Judge (Gemini Flash, ~20% of patch attempts, ~300 firings) | ~$10 |
| Feedback synthesis (Haiku on FAIL + judge REJECT) | ~$3 |
| Evolution cycle LLM calls (rubric edits, pattern extraction) | ~$5 |
| Optional MIPROv2 pass (30 validation patches, ~200 LLM calls) | ~$15 |
| Debugging / reruns buffer | ~$40 |
| **Total** | **~$180** |

Lower than v2's $235 because reproduction test generation is amortized across retries (old v2 re-ran the full cascade on every retry).

Context caching on issue text + file skeletons via Gemini API reduces judge cost by ~80% if enabled; the budget above assumes conservative uncached pricing.

---

## 10. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Phase A buggy-repo gate drops everything (zero-signal UNCERTAIN too common) | Loosen bucket C acceptance; add mask 4 (paraphrase) if gate rate > 40% |
| UNCERTAIN rate is too high (> 35% of patches), judge becomes de facto primary gate | Tighten PASS threshold to 0.65; re-examine bucket weights; may indicate generator too conservative |
| Judge consistently picks UNCERTAIN on its own verdict | Add N=3 self-consistency voting on verdict only; check for prompt ambiguity |
| Judge accuracy on labeled UNCERTAIN patches < 65% | Fall back to per-instance rubric generation using Abstain-and-Validate two-call pattern |
| Evolution overfits the slice, held-out regresses | Keep held-out set untouched; revert non-generalizing edits |
| MIPROv2 stretch task consumes too much budget | Hard cap at 200 LLM calls; skip if over budget |
| Gemini Flash rate limits during evaluation runs | Batch requests, add retry-with-backoff; have Haiku as fallback for judge |

---

## 11. What "Success" Looks Like (Updated)

**Minimum (passing grade):**
- Full cascade including reproduction + judge works end-to-end on 50 instances
- Per-layer contribution analysis showing what each of the four layers catches
- Honest discussion of UNCERTAIN-rate findings

**Good (A):**
- Measurable Pass@1-with-retries improvement over +Static+Regression baseline
- Judge accuracy on UNCERTAIN-bucket patches > 65% (beats random + a coin flip)
- Evolution cycle produces non-regressive improvements on held-out
- 3+ detailed case studies

**Great (A+ / workshop paper seed):**
- Evolution generalizes to held-out (no slice-overfitting)
- False PASS rate at Layer 3 measurably lower than at Layer 2 alone — concrete evidence the judge closes a real gap
- Failure-pattern library shows transferable patterns across repositories
- MIPROv2 or equivalent optimization provides additional lift

---

## 12. New References (Beyond v2)

Beyond the references in v2, the following anchor the new design decisions:

### LLM-as-a-Judge for patches
- **Agentic Rubrics** — Raghavendra et al., arXiv:2601.04171, 2026 — 4-axis YAML rubric schema, weight/binary grading, 54.2% Best@16
- **R2E-Gym** — Jain et al., arXiv:2504.07164, 2025 — hybrid verifier lifting 43.7 → 51.0 Best@26, agent-sentiment attention bias
- **Abstain and Validate** — Cambronero et al., arXiv:2510.03217, 2025 — two-call fix-spec-then-grade for rubric generation without ground truth
- **Rubrics as Rewards** — Gunjal et al., arXiv:2507.17746, 2025 — small judges benefit more from rubrics
- **RLCF / Checklists Are Better Than Reward Models** — arXiv:2507.18624, 2025 — candidate-divergence checklist generation
- **CodeJudge** — Tong & Zhang, EMNLP 2024, arXiv:2410.02184 — taxonomy-guided fault localization, CoT-hurts-judges finding
- **CodeJudgeBench** — arXiv:2507.10535, 2025 — 26 judges benchmarked, position bias quantified
- **Prometheus** — arXiv:2310.08491, 2023 — reference answer as score-5 exemplar

### Bias and calibration
- **MT-Bench position bias** — Zheng et al., arXiv:2306.05685
- **Self-preference in LLM judges** — Panickssery et al., arXiv:2404.13076, 2024
- **Cognitive-bias cue drops** — arXiv:2508.11278, 2025
- **TH-Score verbalized confidence** — arXiv:2508.06225, 2025

### Self-evolution
- **EvalGen / criteria drift** — Shankar et al., UIST 2024, arXiv:2404.12272
- **SAFE / AutoVerus** — arXiv:2410.15756, 2025; arXiv:2409.13082, 2024
- **Recursive Rubric Decomposition** — arXiv:2602.05125, 2026
- **MIPROv2 / DSPy** — arXiv:2412.15298, 2024
- **Evidently AI MIPROv2 case study** — 64% → 96% judge accuracy
- **ExpeRepair** — arXiv:2506.10484, 2025 — dual-memory ADD/REMOVE/UPDATE operations

### Semantic-correctness gap
- **PatchDiff** — Wang et al., arXiv:2503.15223, ICSE 2026 — 29.6% behavioral divergence
- **SWE-Bench+** — Aleithan et al., arXiv:2410.06992, ICLR 2026 — 31.08% suspicious patches
- **SWE-ABS** — arXiv:2603.00520, 2026 — 19.78% rejection rate with strengthened tests
- **Team Atlanta** — team-atlanta.github.io, 2026 — "Right Root Cause, Wrong Patches" taxonomy

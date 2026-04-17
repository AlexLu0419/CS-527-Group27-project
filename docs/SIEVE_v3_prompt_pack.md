# SIEVE v3 — Initial Prompt Pack for LLM Components

This file contains initial prompts for every LLM-powered component in SIEVE v3. Each prompt is grounded in published SOTA work and annotated with its source, suggested model/temperature, and output schema. Treat these as **starting points to iterate on**, not final versions — §7 at the end describes the iteration protocol.

Components covered:

1. Issue preparation — extract structured fields from raw issue text
2. Test file localization — locate the test file to target
3. Focal function localization — locate the function under test
4. Reproduction test generator (3 masks: raw / traceback-first / snippet-first)
5. Feedback synthesizer — convert failing test into actionable feedback
6. LLM-as-a-Judge — 4-item rubric grading on UNCERTAIN verdicts
7. Evolution: failure-pattern extractor — mine error patterns from labeled slice

---

## 1. Issue Preparation

**Purpose:** Parse an unstructured issue into typed fields that downstream prompts can anchor on. This is a non-agentic preprocessing call that runs once per issue.

**SOTA grounding:** Issue2Test (arXiv:2503.16320) and Otter (arXiv:2502.05368) both decompose issues into structured components before generation. The motivation is that reproduction tests that target the issue's *structured signal* (traceback exception type, reporter's snippet, expected-vs-actual pair) outperform tests generated from the blob.

**Model:** Gemini Flash or Claude Haiku. **Temperature:** 0. **Output:** JSON mode with schema.

```
SYSTEM: You are an issue parser. Given a raw GitHub issue, extract structured fields.
Only extract information that is explicitly present in the issue. Do not infer or synthesize.

USER: Parse the following issue into the requested JSON schema.

--- BEGIN ISSUE ---
{issue_title}

{issue_body}
--- END ISSUE ---

Extract the following fields:

- one_line_summary: a single sentence describing the bug
- traceback: if the issue contains a stack trace, extract it verbatim. Otherwise null.
- exception_type: if an exception type is explicitly named (e.g. ValueError, AttributeError), return it. Otherwise null.
- reporter_snippet: if the reporter included runnable code that triggers the bug, extract it verbatim. Otherwise null.
- expected_behavior: what the reporter says should happen. Quote or paraphrase from the issue.
- actual_behavior: what the reporter says actually happens. Quote or paraphrase from the issue.
- affected_modules: list of module or file paths mentioned in the issue (or in the traceback). Empty list if none.

Return only valid JSON matching the schema. Do not wrap in markdown.
```

**Output schema:**
```json
{
  "one_line_summary": "string",
  "traceback": "string or null",
  "exception_type": "string or null",
  "reporter_snippet": "string or null",
  "expected_behavior": "string",
  "actual_behavior": "string",
  "affected_modules": ["string"]
}
```

**Iteration notes:** The most common failure mode is the parser inferring details not in the issue (hallucinating an exception type, synthesizing a snippet from prose). If that happens, add explicit negative examples to the prompt from your validation slice.

---

## 2. Test File Localization

**Purpose:** Identify which existing test file in the repo is the most appropriate place for a reproduction test to live. Used only to provide the generator with the correct import paths and test-class conventions — not to determine where the test will be executed (reproduction tests are always standalone scripts).

**SOTA grounding:** Otter's two-call localization with minimum-edit-distance repair on hallucinated filenames. The key insight: invalid filenames are *repaired* to the nearest valid name in the repo, not dropped — this prevents downstream stages from starving.

**Model:** Claude Haiku or Gemini Flash. **Temperature:** 0. **Output:** JSON with file path + confidence.

```
SYSTEM: You are a software engineer familiar with this repository's structure.
Your job is to identify the test file that would be the natural home for a new test
reproducing the issue described below.

USER: Given the issue and the repository structure, identify the single most likely
test file where a reproduction test for this issue would live.

--- ISSUE SUMMARY ---
{one_line_summary}

Affected modules: {affected_modules}
Exception type: {exception_type}
--- END SUMMARY ---

--- REPOSITORY TEST DIRECTORY STRUCTURE ---
{test_dir_listing}
--- END STRUCTURE ---

Return the single most likely test file path, relative to the repo root.
If multiple candidates are equally plausible, return the one whose name best matches
the module mentioned in the issue.

If no existing test file is an obvious match, return the test directory where a new
test file would most naturally be created, with is_new_file: true.
```

**Output schema:**
```json
{
  "test_file_path": "string (relative path)",
  "is_new_file": "boolean",
  "confidence": "low | medium | high",
  "rationale": "string, <= 20 words"
}
```

**Post-processing (rule-based, not LLM):** After the LLM returns, validate the path exists in the repo. If not, compute minimum edit distance to all actual test file paths and replace with the closest match if edit distance ≤ 3, else mark as `is_new_file: true` and point at the parent test directory.

---

## 3. Focal Function Localization

**Purpose:** Identify the function/class/method whose behavior the reproduction test should exercise. This anchors the generator to the right code surface.

**SOTA grounding:** Otter's second localization call + Agentless's hierarchical file-to-element retrieval. Passing a concrete function name into the generator prompt measurably reduces hallucinated import paths.

**Model:** Claude Haiku or Gemini Flash. **Temperature:** 0.

```
SYSTEM: You are a software engineer investigating a bug report.
Your job is to identify the specific function, method, or class whose behavior is
directly responsible for the reported bug.

USER: Given the issue and the file-level skeleton of the most likely affected module,
identify the single function, method, or class most directly responsible for the bug.

--- ISSUE ---
{one_line_summary}

Expected: {expected_behavior}
Actual: {actual_behavior}

Traceback (if present):
{traceback}

Reporter's snippet (if present):
{reporter_snippet}
--- END ISSUE ---

--- FILE SKELETON ({focal_file_path}) ---
{file_skeleton}
--- END SKELETON ---

The file skeleton contains only signatures and top-level definitions (no bodies).
Identify the single function/method/class that the test should exercise.
If the bug spans multiple functions, pick the innermost (deepest in the call stack).
```

**Output schema:**
```json
{
  "symbol_name": "string (e.g. ClassName.method_name)",
  "symbol_kind": "function | method | class",
  "import_path": "string (how to import this symbol)",
  "rationale": "string, <= 30 words"
}
```

**Post-processing:** Verify the symbol exists in the file. If the LLM returns a name not present, run min-edit-distance repair against the AST-extracted symbol list from the file (same protocol as file localization).

---

## 4. Reproduction Test Generator (3 masks)

All three masks share the same base structure, adapted from Agentless's canonical prompt (verbatim at `agentless/test/generate_reproduction_tests.py` lines 18–64). The Agentless template has been empirically validated on SWE-bench Lite at a 44% plausibility rate across 40 samples. The three masks differ only in which sections of the issue context they include.

**Model for all three:** Claude Sonnet or equivalent coding-specialized model. **Temperature:** 0.5 (Agentless uses 0.8; we use 0.5 because structural diversity is already induced by the masks). **Samples per mask:** 2.

### 4a. Mask 1 — Raw issue

**When to use:** Always. This is the baseline.

```
SYSTEM: You are a software engineer writing a standalone test that reproduces a bug reported in an issue.

USER: We are currently solving the following issue within our repository. Here is the issue text:

--- BEGIN ISSUE ---
{issue_title}

{issue_body}
--- END ISSUE ---

Please generate a complete test that can be used to reproduce the issue.

The complete test should contain the following:
1. Necessary imports (assume the repository is on the Python path)
2. Code to reproduce the issue described in the issue text
3. Print "Issue reproduced" if the outcome indicates that the issue is reproduced
4. Print "Issue resolved" if the outcome indicates that the issue has been successfully resolved
5. Print "Other issues" if the outcome indicates there are other issues with the source code

Additionally, the script MUST exit with:
- exit code 2 if "Issue reproduced" was printed
- exit code 0 if "Issue resolved" was printed
- exit code 1 if "Other issues" was printed

Here is an example:

```python
from sqlfluff import lint
import sys

def test__rules__std_L060_raised() -> None:
    try:
        sql = "SELECT IFNULL(NULL, 100), NVL(NULL,100);"
        result = lint(sql, rules=["L060"])
        assert len(result) == 2
    except Exception:
        print("Other issues")
        sys.exit(1)
    try:
        assert result[0]["description"] == "Use 'COALESCE' instead of 'IFNULL'."
        assert result[1]["description"] == "Use 'COALESCE' instead of 'NVL'."
        print("Issue resolved")
        sys.exit(0)
    except AssertionError:
        print("Issue reproduced")
        sys.exit(2)

test__rules__std_L060_raised()
```

Constraints:
- Use only Python standard library plus the repository's own modules.
- Do not import from pytest fixtures or conftest.py.
- The script must be runnable with `python reproduce_bug.py` with no arguments.
- The test targets the symbol: {symbol_name} (imported from {import_path}).

Wrap the complete test in ```python...```.
```

### 4b. Mask 2 — Traceback-first

**When to use:** Only if `traceback` field from Stage 1 is non-null.

```
SYSTEM: You are a software engineer writing a standalone test that reproduces a bug, anchoring on a specific exception trajectory.

USER: The following issue was reported. A traceback is available — your test should
reproduce the exact exception trajectory shown in the traceback.

--- ISSUE SUMMARY ---
{one_line_summary}
--- END SUMMARY ---

--- TRACEBACK ---
{traceback}
--- END TRACEBACK ---

Please generate a complete test that:
1. Exercises the code path shown in the traceback
2. Uses `with pytest.raises({exception_type})` or equivalent to catch the specific exception
3. Additionally asserts that the exception message or the failure mode matches what the issue describes
4. Prints "Issue reproduced" / "Issue resolved" / "Other issues" following the same convention as below
5. Exits with codes 2 / 0 / 1 respectively

[embedded example from Mask 1 goes here]

Constraints:
- Use only Python standard library plus the repository's own modules.
- Focus the test on the exception trajectory, not on broader behavior.
- The test targets the symbol: {symbol_name} (imported from {import_path}).

Wrap the complete test in ```python...```.
```

### 4c. Mask 3 — Snippet-first

**When to use:** Only if `reporter_snippet` field from Stage 1 is non-null. Falls back to Mask 1 if absent.

```
SYSTEM: You are a software engineer writing a standalone test that reproduces a bug, starting from the reporter's own example code.

USER: The following issue was reported, and the reporter included a code snippet that triggers the bug.

--- ISSUE SUMMARY ---
{one_line_summary}

Expected: {expected_behavior}
Actual: {actual_behavior}
--- END SUMMARY ---

--- REPORTER'S SNIPPET ---
{reporter_snippet}
--- END SNIPPET ---

Please generate a complete test that:
1. Mirrors the structure of the reporter's snippet as closely as possible
2. Adds explicit assertions for the expected behavior (not just exception catching — compare the produced value against what the issue says it should be)
3. Prints "Issue reproduced" / "Issue resolved" / "Other issues" following the same convention as below
4. Exits with codes 2 / 0 / 1 respectively

[embedded example from Mask 1 goes here]

Constraints:
- Use only Python standard library plus the repository's own modules.
- Stay faithful to the reporter's API calls — don't substitute similar-but-different methods.
- The test targets the symbol: {symbol_name} (imported from {import_path}).

Wrap the complete test in ```python...```.
```

**Iteration notes for all three masks:**
- If buggy-repo gate drop rate exceeds 40%, the most common failure is `ImportError` — tighten the import-path field passed in, or add "verify that `from {import_path} import {symbol_name}` works" as an explicit pre-check in the prompt.
- If the tests pass on the buggy repo (false fail-to-pass), the assertions are usually too weak. For Mask 2, add: "the `pytest.raises` alone is not sufficient — also assert properties of the exception message or traceback frames." For Mask 3, add: "the assertion must compare produced values to explicit expected values; `assert result is not None` is too weak."

---

## 5. Feedback Synthesizer

**Purpose:** On FAIL verdicts, convert a failing reproduction test's raw traceback into an actionable scenario description for mini-swe-agent. The key constraint: describe the *scenario*, not the test itself, to prevent agent overfitting to the specific test.

**SOTA grounding:** SpecRover (arXiv:2408.02232) introduced the idea of reviewer feedback as a natural-language explanation that leads the patching agent to rectify the patch rather than game the test. Their reviewer prompt is adapted below for the feedback-only use case.

**Model:** Claude Haiku or Gemini Flash. **Temperature:** 0.

```
SYSTEM: You are a software engineering reviewer. A patch has been produced to address an issue, but a reproduction test is still failing on the patched code. Your job is to explain WHY the patch is insufficient, in terms of the issue's underlying behavior — not in terms of the test itself.

The engineer receiving this feedback will rewrite the patch. If you describe the specific test they failed, they will overfit to that test. Instead, describe the scenario the test represents and what the patch is still getting wrong about the reported behavior.

USER: Here is the issue that was reported:

--- ISSUE ---
{one_line_summary}

Expected: {expected_behavior}
Actual (before patch): {actual_behavior}
--- END ISSUE ---

Here is the candidate patch (unified diff):

--- PATCH ---
{patch_diff}
--- END PATCH ---

Here is the failing test's outcome on the patched code:

Exit code: {exit_code}
Stdout: {stdout}
Stderr: {stderr}
Failing assertion: {assertion_message}

Write feedback in 2-4 sentences, following this structure:
1. State the scenario the test exercises (in behavioral terms, e.g. "when the input is X, the expected output is Y, but your patch still produces Z")
2. Identify the most likely conceptual gap in the patch (e.g. missed edge case, wrong code location, symptom-only fix)
3. Give one concrete direction for the next attempt

Do not quote the test code. Do not reference the test file name. Describe the scenario in natural language.
```

**Output schema:**
```json
{
  "scenario": "string, 1 sentence",
  "conceptual_gap": "string, 1 sentence",
  "direction": "string, 1 sentence"
}
```

The three fields are concatenated with newlines to form the feedback message passed to mini-swe-agent.

**Iteration notes:** Measure retry Pass@1 lift with this feedback vs. raw traceback as the baseline — the target is +5–10 percentage points (SpecRover reports similar gains). If feedback is too generic ("the patch is incomplete"), add: "cite the specific expected value and the value the patch produces" as an explicit instruction.

---

## 6. LLM-as-a-Judge (Tiebreaker)

**Purpose:** Grade a candidate patch on 4 atomic rubric items when the reproduction layer emits UNCERTAIN.

**SOTA grounding:** Fuses three lines of work:
- **Agentic Rubrics** (Raghavendra et al., arXiv:2601.04171, Scale AI 2026): 4-axis YAML rubric, binary per-item grading, weighted-average aggregation `S = Σ(wᵢsᵢ)/Σwᵢ`, ROC-AUC 0.886 on SWE-Bench Verified. The grading prompt: *"You are a rubric based evaluator for software-engineering agent's generated patch... Emit a score for each rubric item. The score should be a binary score of 1 if the patch satisfies the rubric item and 0 otherwise. Return the scores in a JSON format."*
- **SpecRover reviewer** (arXiv:2408.02232, Figure 4): dual-role prompt evaluating both test and patch, explicitly telling the judge "both the test and the patch may be wrong."
- **R2E-Gym** (arXiv:2504.07164): strip agent trajectory to avoid sentiment-contamination bias.

**Model:** Gemini 2.5 Flash (measured ~20% self-preference vs. ~33% baseline; best choice for neutral judgment). **Temperature:** 0. **Output:** JSON mode with response schema.

```
SYSTEM: You are a rubric-based evaluator for a software engineering agent's candidate patch. You will grade the patch against 4 binary rubric items. Both the patch and the reproduction tests may be wrong — evaluate them independently.

Return your evaluation as JSON matching the provided schema. For each rubric item, return 1 if the patch satisfies the item, 0 otherwise. Provide brief reasoning before the verdict.

USER: An issue was reported. An engineer produced a candidate patch. Reproduction tests were generated independently of the patch; some passed on the patched code and some failed. Your job is to decide whether this patch should be accepted.

--- ISSUE ---
{issue_title}

{issue_body}
--- END ISSUE ---

--- CANDIDATE PATCH (unified diff) ---
{patch_diff}
--- END PATCH ---

--- PRE-PATCH CODE CONTEXT (±30 lines around each hunk) ---
{pre_patch_context}
--- END CONTEXT ---

--- REPRODUCTION TESTS ---

Passing tests on patched code (names only):
{passing_test_names_and_summaries}

Failing tests on patched code (full code + traceback for each):
{failing_tests_full}
--- END REPRODUCTION TESTS ---

Grade the patch on the following 4 rubric items. Each is binary (0 or 1). Verb-first rubric items:

1. `root_cause`: Modifies code in a region that plausibly addresses the root cause described in the issue. (Weight: 3)

2. `not_symptom_only`: Addresses the reported behavior rather than only suppressing its visible symptom. The patch FAILS this item if any of the following is true:
   - Hardcodes a specific value from the issue report rather than fixing the condition that produced it
   - Catches an exception silently with bare `except:` or `except Exception: pass`
   - Adds a guard for only the exact input shape mentioned in the issue without handling the class of inputs
   (Weight: 3)

3. `no_scope_creep`: Makes only changes necessary for the issue, without broad refactors, unrelated API changes, mass renames, or weakening of existing tests. (Weight: 2)

4. `failing_tests_legitimate`: For the failing reproduction tests above, the majority (>50%) are legitimate reproductions of the issue rather than brittle, flaky, or testing the wrong thing. Grade each failing test individually before rolling up. (Weight: 3)

For item 4, additionally return a per-test assessment list.

Then provide:
- overall verdict: ACCEPT, REJECT, or UNCERTAIN
- confidence: low, medium, or high
- brief reasoning (<= 5 sentences)

Note: this is a tiebreaker evaluation. When in doubt, prefer UNCERTAIN over ACCEPT.
```

**Output schema (Gemini `response_schema`):**
```json
{
  "rubric_scores": {
    "root_cause": 0,
    "not_symptom_only": 0,
    "no_scope_creep": 0,
    "failing_tests_legitimate": 0
  },
  "failing_test_assessments": [
    {
      "test_name": "string",
      "legitimate": 0,
      "reason": "string (<= 20 words)"
    }
  ],
  "reasoning": "string (<= 5 sentences)",
  "verdict": "ACCEPT | REJECT | UNCERTAIN",
  "confidence": "low | medium | high"
}
```

**Aggregation rule (non-LLM, in code):**
```python
S = (3*scores["root_cause"] + 3*scores["not_symptom_only"]
     + 2*scores["no_scope_creep"] + 3*scores["failing_tests_legitimate"]) / 11

# Hard gates: items 1 and 2 are non-negotiable
if scores["root_cause"] == 0 or scores["not_symptom_only"] == 0:
    return "FAIL"
if verdict == "REJECT":
    return "FAIL"
if verdict == "ACCEPT" and confidence == "high" and S >= 0.75:
    return "PASS"
return "UNCERTAIN"
```

**Critical input-hygiene rules (apply before constructing the prompt):**
- Strip any agent trajectory, chain-of-thought, or self-narration from all inputs
- Remove patch-level comments that describe the patch (e.g. "Fix for issue #1234")
- Remove author/provenance metadata from the diff
- Do NOT include static analysis output or regression test results — those are consumed by earlier layers

**Iteration notes:** If the judge accuracy on labeled UNCERTAIN patches is below 65%, the most likely fix is adding concrete negative examples to item 2 (symptom-only fix). Pull real false-PASS cases from your validation slice and embed them as "items the patch FAILS this test if it does X, as in this example: [diff snippet]". Second iteration target: MIPROv2 prompt optimization on 30 labeled patches (Evidently case study: 64% → 96%).

---

## 7. Failure-Pattern Extractor (Self-Evolution)

**Purpose:** After each evolution cycle, mine the labeled validation slice for transferable failure patterns to add to the semantic memory (see SIEVE v3 §5.3).

**SOTA grounding:** ExpeRepair (arXiv:2506.10484) dual-memory ADD/REMOVE/UPDATE with capped operations. EvalGen (UIST 2024) "criteria drift" — evaluation criteria themselves must evolve from observed outputs.

**Model:** Claude Sonnet or equivalent (this is a low-volume analytical call — frontier model worth the cost). **Temperature:** 0.3. **Output:** JSON list of patterns.

```
SYSTEM: You are analyzing errors made by an automated patch-verification system. Your job is to identify transferable failure patterns — generalizable classes of patches that the system incorrectly accepted or rejected — and propose concrete detection rules that would catch them.

USER: Below are {N} labeled examples where the SIEVE system's verdict disagreed with the ground-truth outcome. Each example has the issue, the patch, the system's verdict, and the correct label.

--- LABELED EXAMPLES ---
{labeled_examples}
--- END EXAMPLES ---

Group these examples into failure patterns. For each pattern, provide:

1. `pattern_id`: short snake_case identifier (e.g. symptom_suppression_none_return)
2. `description`: 1-sentence description of the pattern
3. `error_type`: "false_accept" (system said PASS, actual FAIL) or "false_reject" (system said FAIL, actual PASS)
4. `example_instance_ids`: which of the labeled examples exhibit this pattern
5. `detection_heuristic`: a concrete rule that would detect this pattern. Prefer rules that are one of:
   - a regex on the patch diff
   - a semantic check describable in one line (e.g. "patch adds a try/except around the reported call")
   - a rubric-item addition that would catch it
6. `proposed_intervention`: one of {"add_rubric_item", "modify_rubric_item_2_examples", "add_mask_instruction", "modify_feedback_prompt", "add_semgrep_rule"} — which part of SIEVE should change to address this pattern

Return a JSON list. Only include patterns that cover at least 2 examples (single-example patterns are likely noise). Cap the list at 5 patterns — if you find more, keep the 5 with the largest example_instance_ids lists.
```

**Output schema:**
```json
[
  {
    "pattern_id": "string",
    "description": "string",
    "error_type": "false_accept | false_reject",
    "example_instance_ids": ["string"],
    "detection_heuristic": "string",
    "proposed_intervention": "string"
  }
]
```

**How to use the output:** Each returned pattern gets reviewed manually (a few minutes each). Patterns where `proposed_intervention` is actionable and the `detection_heuristic` generalizes get applied. Revert any intervention that regresses the held-out set (SIEVE v3 §5.2).

---

## 8. Iteration Protocol for All Prompts

The prompts above are v0. Each component evolves independently via a three-step cycle:

1. **Measure on validation slice.** Run SIEVE end-to-end on the 50-patch labeled slice. For each component, compute the relevant metric:
   - Issue parser: field-extraction accuracy (manual spot check on 20 issues)
   - Localizers: top-1 accuracy against ground-truth patch file
   - Generators: F→P rate per mask
   - Feedback synthesizer: retry Pass@1 lift vs. raw-traceback baseline
   - Judge: ACCEPT/REJECT accuracy on UNCERTAIN-bucket patches, calibration of confidence
   - Evolution extractor: generalization rate of proposed interventions on held-out

2. **Diagnose.** For each component below its target, pull 5–10 failure cases and categorize: is it a prompt issue (unclear instruction, missing negative example), a model issue (capability insufficient — upgrade Haiku → Sonnet), or a data issue (garbage input from an upstream stage)?

3. **Intervene.** Three escalation tiers in order of cost:
   - **Tier 1 — manual edit**: add a negative example, tighten a constraint, rephrase an ambiguous line. Iterate 2–3 times per component.
   - **Tier 2 — MIPROv2/DSPy**: wrap the prompt in a DSPy program, run Bayesian search over instruction + few-shot combinations with 30 labeled examples. Expect 20–30 percentage point gains on the noisier components (Evidently AI case study reports 64% → 96% on a judge, which is the high end). Budget: ~$15 in LLM calls per component.
   - **Tier 3 — architecture change**: if a component is fundamentally underperforming after Tier 1+2, revisit its design. Examples: switch fixed judge rubric to per-instance Agentic Rubrics (§4.6 risks table in SIEVE v3); add a mask 4 (AssertFlip) to the generator; split localization into 3 calls instead of 2.

**Budget guidance:** Don't iterate all components in parallel. The judge has the highest impact per improvement dollar — iterate it first. The issue parser is usually fine at v0 — iterate it last. Generators are mid-priority: tune Mask 1 first (it runs always), then 2 and 3.

**Stop criterion:** A component is "done enough" for the course project when one more iteration produces < 2 percentage points of improvement on held-out. Document what you did and move on.

---

## 9. References for Prompt Sources

- **Agentless generator prompt**: `agentless/test/generate_reproduction_tests.py`, lines 18–64. Xia et al., arXiv:2407.01489, 2024.
- **Agentic Rubrics grading prompt**: Raghavendra et al., arXiv:2601.04171, 2026 (Appendix A.10 cites the verbatim grader prompt).
- **SpecRover reviewer template**: Ruan, Zhang, Roychoudhury, ICSE 2025, Figure 4. Reproduced and adapted above for the feedback-only use case.
- **CodeMonkeys exit-code convention**: Ehrlich et al., arXiv:2501.14723, 2025. Tests exit with code 2 when issue is present, 0 when resolved.
- **Otter two-call localization with edit-distance repair**: Ahmed et al., arXiv:2502.05368, 2025.
- **R2E-Gym agent-trajectory stripping**: Jain et al., arXiv:2504.07164, 2025. Removing trajectory drops Best@26 from 42.8 → 37.6, confirming the bias.
- **ExpeRepair ADD/REMOVE/UPDATE memory operations**: arXiv:2506.10484, 2025.
- **MIPROv2 / DSPy prompt optimization**: arXiv:2412.15298, 2024. Evidently AI case study: 64% → 96% judge accuracy.

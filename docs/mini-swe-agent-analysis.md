# mini-swe-agent Codebase Analysis for EvoEval

## 1. Repository Directory Structure

```
mini-swe-agent/
├── pyproject.toml                          # Package config, entry points, dependencies
├── mkdocs.yml                              # Documentation site config
├── README.md
├── LICENSE.md
│
├── docs/                                   # MkDocs documentation source
│   └── assets/
│
├── src/minisweagent/
│   ├── __init__.py                         # Exports Environment, Model, __version__
│   ├── exceptions.py                       # InterruptAgentFlow, LimitsExceeded
│   │
│   ├── agents/
│   │   ├── __init__.py                     # get_agent() factory function
│   │   ├── default.py                      # ★ DefaultAgent — the core ~100-line agent
│   │   └── interactive.py                  # InteractiveAgent — adds user-in-the-loop
│   │
│   ├── models/
│   │   ├── __init__.py                     # get_model() factory
│   │   ├── litellm_model.py               # ★ LitellmModel — main model adapter (OpenAI, Anthropic, etc.)
│   │   ├── openrouter_model.py            # OpenRouter-specific adapter
│   │   ├── requesty_model.py              # Requesty adapter
│   │   ├── portkey_model.py               # Portkey adapter
│   │   └── utils/
│   │       ├── retry.py                    # Retry logic for API calls
│   │       └── ...
│   │
│   ├── environments/
│   │   ├── __init__.py                     # get_environment() factory
│   │   ├── local.py                        # ★ LocalEnvironment — subprocess.run execution
│   │   ├── docker.py                       # ★ DockerEnvironment — docker exec execution
│   │   ├── singularity.py                  # Singularity/Apptainer backend
│   │   ├── bubblewrap.py                   # Bubblewrap sandbox
│   │   ├── contree.py                      # Contree backend
│   │   └── swerex_*.py                     # SWE-ReX backends
│   │
│   ├── config/
│   │   ├── __init__.py                     # builtin_config_dir, get_config_from_spec()
│   │   ├── benchmarks/
│   │   │   └── swebench.yaml              # ★ Default SWE-bench config (prompts, limits, env)
│   │   └── ...
│   │
│   ├── run/
│   │   ├── hello_world.py                  # Minimal example script
│   │   ├── mini.py                         # CLI entry point for `mini` command
│   │   └── benchmarks/
│   │       ├── swebench.py                # ★ Batch SWE-bench runner (parallelized)
│   │       ├── swebench_single.py         # ★ Single-instance SWE-bench runner
│   │       └── utils/
│   │           └── batch_progress.py       # Rich progress bar manager
│   │
│   └── utils/
│       ├── serialize.py                    # recursive_merge(), UNSET sentinel
│       └── log.py                          # Logging helpers
│
└── tests/                                  # Test suite
```


## 2. Key Files & Code Snippets You Will Use

### 2.1 `src/minisweagent/agents/default.py` — The Core Agent

**Responsibility:** The ~155-line agent loop. Manages the system→user→assistant→observation message cycle. This is the class you will subclass or wrap.

**Key code:**

```python
class AgentConfig(BaseModel):
    system_template: str           # Jinja2 template for system prompt
    instance_template: str         # Jinja2 template for task description
    step_limit: int = 0            # Max LLM calls (0 = unlimited)
    cost_limit: float = 3.0        # Max $ spend per instance
    output_path: Path | None = None

class DefaultAgent:
    def __init__(self, model: Model, env: Environment, *, config_class=AgentConfig, **kwargs):
        self.config = config_class(**kwargs)
        self.messages: list[dict] = []
        self.model = model
        self.env = env

    def run(self, task: str = "", **kwargs) -> dict:
        """Main loop: init messages, then step() until exit."""
        # Adds system + instance messages, then loops step()
        # Returns last message's extra dict (contains exit_status, submission)

    def step(self) -> list[dict]:
        """One iteration: query LLM → execute actions."""
        return self.execute_actions(self.query())

    def query(self) -> dict:
        """Call LLM, check limits, add response to messages."""
        # Raises LimitsExceeded if step/cost limits hit

    def execute_actions(self, message: dict) -> list[dict]:
        """Run bash commands from LLM response, format observations."""
        outputs = [self.env.execute(action) for action in message["extra"]["actions"]]
        return self.add_messages(*self.model.format_observation_messages(...))

    def serialize(self, *extra_dicts) -> dict:
        """Full trajectory as JSON-serializable dict."""

    def save(self, path, *extra_dicts) -> dict:
        """Write trajectory to .traj.json file."""
```

**How you'll use it:** Your `agent_with_feedback.py` wraps this class. On EvoEval rejection, you reinitialize with the feedback injected into the instance template.

---

### 2.2 `src/minisweagent/models/litellm_model.py` — Model Adapter

**Responsibility:** Wraps litellm to call any LLM (OpenAI, Anthropic, Together AI, local vLLM). Handles message formatting, tool-call parsing, cost tracking.

**Key code:**

```python
class LitellmModelConfig(BaseModel):
    model_name: str                  # e.g. "openai/gpt-4o-mini", "together_ai/Qwen/..."
    model_kwargs: dict = {}          # temperature, drop_params, etc.
    cost_tracking: str = "default"

class LitellmModel:
    def query(self, messages: list[dict]) -> dict:
        """Call litellm.completion(), return formatted message dict."""
        # Returns: {"role": "assistant", "content": ..., "extra": {"actions": [...], "cost": ...}}

    def format_message(self, role, content, extra=None) -> dict:
        """Create a message dict in the agent's format."""

    def format_observation_messages(self, message, outputs, template_vars) -> list[dict]:
        """Format environment outputs as user messages for next LLM turn."""
```

**How you'll use it:** Direct usage for the coding agent (Qwen2.5-Coder via Together AI). Also used independently in Layer 2 (checker generation) and Layer 3 (LLM judge) via litellm's unified API.

---

### 2.3 `src/minisweagent/environments/docker.py` — Docker Environment

**Responsibility:** Executes bash commands inside Docker containers via `docker exec`. Each SWE-bench instance runs in a pre-built Docker image containing the repo at the correct commit.

**Key interface:**

```python
class DockerEnvironment:
    def execute(self, action: str) -> dict:
        """Run a bash command in the container.
        Returns: {"output": str, "returncode": int, "exception_info": str|None}
        """

    def get_template_vars(self) -> dict:
        """Template variables like cwd, timeout, etc."""
```

**How you'll use it:** SWE-bench instances run in Docker. After the agent produces a patch, you extract the `submission` (git diff) from the agent's final message for EvoEval verification.

---

### 2.4 `src/minisweagent/config/benchmarks/swebench.yaml` — SWE-bench Config

**Responsibility:** The complete configuration for SWE-bench runs — system prompt, instance template (task instructions), environment settings, model settings.

**Key sections:**

```yaml
agent:
  system_template: |
    You are a helpful assistant that can interact with a computer shell...
  instance_template: |
    <pr_description>{{task}}</pr_description>
    <instructions>
    # Task Instructions
    ...
    ## Submission
    echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt
    </instructions>
  step_limit: 250
  cost_limit: 3.

environment:
  cwd: "/testbed"
  timeout: 60
  environment_class: docker
  env:
    PAGER: cat    # Prevents interactive pagers

model:
  model_name: "anthropic/claude-sonnet-4-5-20250929"
  model_kwargs:
    temperature: 0.0
```

**How you'll use it:** Fork this config, change `model_name` to your chosen model (e.g. `together_ai/Qwen/Qwen2.5-Coder-32B-Instruct`). For feedback retries, modify `instance_template` to append prior rejection reasons.

---

### 2.5 `src/minisweagent/run/benchmarks/swebench.py` — Batch Runner

**Responsibility:** Loads SWE-bench dataset, runs agent on each instance in parallel via ThreadPoolExecutor, writes `preds.json` for evaluation.

**Key functions:**

```python
def process_instance(instance, output_dir, config, progress_manager):
    """Process one SWE-bench instance end-to-end."""
    # 1. Pull Docker image
    # 2. Create agent with ProgressTrackingAgent
    # 3. agent.run(problem_statement)
    # 4. Extract submission (git diff)
    # 5. Save trajectory + update preds.json

def get_sb_environment(config, instance):
    """Build Docker environment for a SWE-bench instance."""
    # Maps instance_id → Docker image name
    # e.g. "django__django-12345" → "swebench/sweb.eval.x86_64.django_1776_django-12345:latest"

def update_preds_file(output_path, instance_id, model_name, result):
    """Thread-safe write to preds.json."""

def filter_instances(instances, filter_spec, slice_spec, shuffle):
    """Filter/slice/shuffle instance list."""
```

**How you'll use it:** This is your starting point for `run_evoeval.py`. You'll wrap `process_instance` to add the EvoEval verification cascade between patch generation and preds.json writing, plus retry logic.

---

### 2.6 `src/minisweagent/run/benchmarks/swebench_single.py` — Single Instance Runner

**Responsibility:** Debug tool — runs agent on one SWE-bench instance with interactive mode.

**How you'll use it:** Essential for debugging during development. Test your EvoEval pipeline on individual instances before batch runs.

---

### 2.7 `src/minisweagent/utils/serialize.py` — Config Merging

**Responsibility:** `recursive_merge()` deeply merges multiple dicts (used for YAML config layering). `UNSET` sentinel for optional overrides.

**How you'll use it:** When building configs programmatically for feedback retries.

---

### 2.8 `src/minisweagent/exceptions.py` — Flow Control

**Responsibility:** `InterruptAgentFlow` (caught by run loop, adds exit messages), `LimitsExceeded` (stops agent when step/cost limits hit).

```python
class InterruptAgentFlow(Exception):
    """Raised to interrupt agent flow with specific messages."""
    def __init__(self, *messages): self.messages = messages

class LimitsExceeded(InterruptAgentFlow):
    """Step or cost limit exceeded."""
```

**How you'll use it:** Understanding these is critical when the agent hits limits — you need to handle this gracefully in your retry logic.


## 3. How to Run mini-swe-agent on SWE-bench (ASCII Flow)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  SETUP                                                                   │
│                                                                          │
│  1. pip install mini-swe-agent                                           │
│  2. pip install datasets sb-cli                                          │
│  3. Set API key:  export OPENAI_API_KEY=...                              │
│     (or TOGETHER_API_KEY, ANTHROPIC_API_KEY, etc.)                       │
│  4. Ensure Docker is running                                             │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  RUN BATCH (B0 baseline — pass@1)                                        │
│                                                                          │
│  mini-extra swebench \                                                   │
│    --model together_ai/Qwen/Qwen2.5-Coder-32B-Instruct \                │
│    --subset verified \                                                   │
│    --split test \                                                        │
│    --slice 0:50 \                                                        │
│    --workers 4 \                                                         │
│    -o results/baseline_b0/                                               │
│                                                                          │
│  (Or use --filter with regex for your 50-instance subset IDs)            │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  OUTPUT FILES                                                            │
│                                                                          │
│  results/baseline_b0/                                                    │
│  ├── preds.json                 ← { instance_id: {model_patch: "..."} }  │
│  ├── minisweagent.log           ← Full debug log                         │
│  ├── exit_statuses_*.yaml       ← Per-instance exit status               │
│  └── <instance_id>/                                                      │
│      └── <instance_id>.traj.json  ← Full trajectory (messages + meta)    │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  EVALUATE (check patches against hidden tests)                           │
│                                                                          │
│  Option A — Cloud (fast, free, recommended):                             │
│    sb-cli submit swe-bench_verified test \                               │
│      --predictions_path results/baseline_b0/preds.json \                 │
│      --run_id evoeval_b0                                                 │
│                                                                          │
│  Option B — Local:                                                       │
│    python -m swebench.harness.run_evaluation \                           │
│      --dataset_name princeton-nlp/SWE-bench_Verified \                   │
│      --predictions_path results/baseline_b0/preds.json \                 │
│      --max_workers 4 --run_id evoeval_b0                                 │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  RESULTS                                                                 │
│                                                                          │
│  → resolve_rate = (# passed) / 50                                        │
│  → This is your B0 number to beat with EvoEval                           │
└─────────────────────────────────────────────────────────────────────────┘
```

### Running with EvoEval (your modified pipeline)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  FOR each SWE-bench instance:                                             │
│                                                                           │
│   ┌─────────────┐      ┌──────────────┐      ┌──────────────────────┐    │
│   │ mini-SWE-   │      │ Extract      │      │   EVOEVAL CASCADE    │    │
│   │ agent.run() │─────▶│ submission   │─────▶│                      │    │
│   │ (LLM loop)  │      │ (git diff)   │      │  L1: Structural      │    │
│   └─────────────┘      └──────────────┘      │  L2: Gen. Checkers   │    │
│         ▲                                     │  L3: LLM Judge       │    │
│         │                                     └──────────┬───────────┘    │
│         │                                                │               │
│         │    feedback                        ┌───────────┴────────┐      │
│         │◀── (rejection ──────────────────── │  ACCEPT / REJECT   │      │
│         │     reason)                        └───────────┬────────┘      │
│         │                                                │               │
│         │  (retry up to 2x)                   ACCEPT ──▶ preds.json      │
│                                                                           │
│   Key integration point:                                                  │
│   • After agent.run(), grab info["submission"] (the git diff)             │
│   • Feed diff to evoeval_evaluate()                                       │
│   • On reject: re-run agent with feedback in instance_template            │
│   • On accept or 3rd attempt: write to preds.json                         │
└──────────────────────────────────────────────────────────────────────────┘
```


## 4. Integration Points: How mini-swe-agent Meets EvoEval

### 4.1 Extracting the Patch

After `agent.run(task)`, the return dict contains:

```python
info = agent.run(instance["problem_statement"])
patch_diff = info.get("submission", "")  # This is the git diff string
```

This `patch_diff` is what you feed into your Layer 1/2/3 checks.

### 4.2 Feeding Rejection Feedback Back

The SWE-bench config's `instance_template` is a Jinja2 template. For retries, you modify the template to include prior feedback:

```python
# In your wrapper around process_instance:
if attempt > 1:
    config["agent"]["instance_template"] = ORIGINAL_TEMPLATE + f"""

    <previous_attempt_feedback>
    Your previous patch was rejected for the following reason:
    {feedback}
    Please address this issue in your next attempt.
    </previous_attempt_feedback>
    """
```

### 4.3 Applying the Patch to the Repo (for Layer 1/2 checks)

The patch is a git diff string. To run Layer 1/2 checks, you apply it inside the Docker container:

```python
# Apply patch in the SWE-bench Docker container
env.execute(f"cd /testbed && echo '{patch_diff}' | git apply --check")  # dry run
env.execute(f"cd /testbed && echo '{patch_diff}' | git apply")          # actual apply
```

Or, for Layer 1 checks that just parse the diff text (syntax, scope, file relevance), you can run them on the host without applying.


## 5. Concrete TODO List (Revised for mini-swe-agent v2)

### Phase 0: Environment Setup [~1 day]

- [ ] **P0-1.** Install mini-swe-agent: `pip install mini-swe-agent`
- [ ] **P0-2.** Install evaluation tools: `pip install datasets swebench sb-cli`
- [ ] **P0-3.** Verify Docker works: run `mini-extra swebench-single --subset lite --split dev -m openai/gpt-4o-mini -i 0` on one instance
- [ ] **P0-4.** Set up API keys: Together AI (for Qwen2.5-Coder), OpenAI (for GPT-4o-mini)
- [ ] **P0-5.** Clone the 50-instance subset list from `github.com/mariushobbhahn/SWEBench-verified-mini`
- [ ] **P0-6.** Create `split.json`: randomly split 50 instances into 15 evolution + 35 evaluation

### Phase 1: Baselines [~2-3 days]

- [ ] **P1-1.** Run B0 (pass@1): batch run on all 50 instances with Qwen2.5-Coder-32B
  ```
  mini-extra swebench \
    --model together_ai/Qwen/Qwen2.5-Coder-32B-Instruct \
    --subset verified --split test \
    --filter "instance_id_1|instance_id_2|..." \
    -o results/b0/ -w 4
  ```
- [ ] **P1-2.** Evaluate B0: `sb-cli submit swe-bench_verified test --predictions_path results/b0/preds.json`
- [ ] **P1-3.** Run B1 (pass@3 random): write a script that runs the agent 3× per instance (no feedback), submits best guess
- [ ] **P1-4.** Evaluate B1
- [ ] **P1-5.** Manually inspect 10 failed patches from B0 → write failure mode analysis
- [ ] **P1-6.** Record baseline numbers in results spreadsheet

### Phase 2: Layer 1 — Structural Checks [~2-3 days]

- [ ] **P2-1.** Write `evoeval/utils/diff_parser.py`: parse git diff to extract modified files, added/deleted line counts, modified function names
- [ ] **P2-2.** Write the 6 Layer 1 checks (syntax, file relevance, test contamination, import integrity, scope, deletion-without-replacement)
- [ ] **P2-3.** Test Layer 1 on all 50 B0 patches: measure true/false rejection rates
- [ ] **P2-4.** Tune thresholds on evolution set (15 instances)
- [ ] **P2-5.** Build retry wrapper around `process_instance`:
  - Extract `submission` from agent result
  - Feed to Layer 1
  - On reject: re-run agent with feedback injected into instance_template
  - Up to 3 total attempts
- [ ] **P2-6.** Run B2 (agent + L1 + retry), evaluate

### Phase 3: Layer 2 — Generated Checkers [~3-4 days]

- [ ] **P3-1.** Write `evoeval/layer2/utility_functions.py`: `read_file()`, `extract_function()`, `extract_class()`, `get_patch_diff()`, `file_exists()`, `count_occurrences()` — all operating inside the Docker container
- [ ] **P3-2.** Write seed meta-prompt v0 (per Section 6.1 of plan)
- [ ] **P3-3.** Write `evoeval/layer2/checker_generator.py`:
  - Call GPT-4o-mini with meta-prompt + issue + source snippets
  - Parse returned Python functions
  - Validate each checker in a sandbox (5-sec timeout)
- [ ] **P3-4.** Write `evoeval/layer2/checker_executor.py`: run checkers against the patched repo in Docker
- [ ] **P3-5.** Test on 5 evolution instances manually, iterate meta-prompt
- [ ] **P3-6.** Write Layer 2 decision logic (pass rate thresholds)
- [ ] **P3-7.** Run B3 (agent + L1 + seed L2 + retry), evaluate

### Phase 4: Meta-Prompt Evolution [~2-3 days]

- [ ] **P4-1.** Write `evolution/fitness.py`: score meta-prompt on 15 evolution instances (requires ground-truth test results)
- [ ] **P4-2.** Write `evolution/mutation_proposer.py`: use GPT-4o-mini to propose 3 meta-prompt variants given failure cases
- [ ] **P4-3.** Write `evolution/evolve_meta_prompt.py`: 5 generations × 3 mutations
- [ ] **P4-4.** Run evolution loop, log trajectory
- [ ] **P4-5.** Run E1 (agent + L1 + evolved L2 + retry) on 35 evaluation instances, evaluate

### Phase 5: Layer 3 + Full System [~1-2 days]

- [ ] **P5-1.** Write `evoeval/layer3/llm_judge.py`: structured GPT-4o-mini judge
- [ ] **P5-2.** Write `evoeval/orchestrator.py`: L1 → L2 → L3 cascade + retry logic
- [ ] **P5-3.** Run E2 (full EvoEval) on 35 evaluation instances, evaluate
- [ ] **P5-4.** Compute cost breakdown per configuration

### Phase 6: Analysis & Paper [~3-5 days]

- [ ] **P6-1.** Comparison table: B0 vs B1 vs B2 vs B3 vs E1 vs E2
- [ ] **P6-2.** Layer distribution analysis: what % of decisions at L1/L2/L3
- [ ] **P6-3.** False rejection analysis
- [ ] **P6-4.** Evolution ablation: seed vs evolved meta-prompt
- [ ] **P6-5.** 3-5 qualitative case studies (good catches)
- [ ] **P6-6.** 2-3 failure case studies
- [ ] **P6-7.** Write paper


## 6. Recommended Project Structure

```
evoeval-project/
├── README.md
├── requirements.txt                     # mini-swe-agent, datasets, openai, sb-cli
├── config/
│   ├── swebench_qwen.yaml              # Forked swebench.yaml with Qwen model
│   └── swebench_qwen_feedback.yaml     # Config variant with feedback template
│
├── data/
│   ├── instance_ids.json               # 50 instance IDs from SWEBench-verified-mini
│   └── split.json                      # {evolution: [...15...], evaluation: [...35...]}
│
├── evoeval/
│   ├── __init__.py
│   ├── orchestrator.py                 # L1 → L2 → L3 cascade + retry loop
│   ├── layer1/
│   │   ├── __init__.py                 # layer1_evaluate() aggregator
│   │   ├── syntax_check.py
│   │   ├── file_relevance.py
│   │   ├── test_contamination.py
│   │   ├── import_integrity.py
│   │   ├── scope_check.py
│   │   └── deletion_check.py
│   ├── layer2/
│   │   ├── __init__.py                 # layer2_evaluate()
│   │   ├── checker_generator.py        # LLM → Python checker functions
│   │   ├── checker_executor.py         # Sandboxed checker execution
│   │   ├── meta_prompt.py              # Meta-prompt loading/versioning
│   │   └── utility_functions.py        # read_file, extract_function, etc.
│   ├── layer3/
│   │   ├── __init__.py
│   │   └── llm_judge.py               # GPT-4o-mini structured judge
│   └── utils/
│       ├── diff_parser.py              # Parse git diffs
│       ├── llm_client.py              # Unified LLM calls + cost tracking
│       └── docker_helpers.py           # Run commands in SWE-bench containers
│
├── evolution/
│   ├── evolve_meta_prompt.py           # Main evolution loop
│   ├── fitness.py                      # Fitness scoring
│   └── mutation_proposer.py            # LLM-based prompt mutation
│
├── scripts/
│   ├── run_baseline_b0.py              # B0: single pass
│   ├── run_baseline_b1.py              # B1: 3 random retries
│   ├── run_evoeval.py                  # E1/E2: full EvoEval pipeline
│   ├── evaluate.py                     # Wrapper around sb-cli
│   └── analyze_results.py             # Generate comparison tables + plots
│
├── results/
│   ├── b0/                             # preds.json + trajectories
│   ├── b1/
│   ├── b2/
│   ├── b3/
│   ├── e1/
│   └── e2/
│
├── meta_prompts/
│   ├── seed_v0.txt                     # Initial meta-prompt
│   ├── evolved_gen1.txt
│   ├── evolved_gen2.txt
│   └── ...
│
└── paper/
    ├── main.tex
    └── figures/
```


## 7. Critical Integration Code Sketch

Here's the key wrapper you need to build around mini-swe-agent:

```python
# scripts/run_evoeval.py — simplified sketch
from minisweagent.agents.default import DefaultAgent
from minisweagent.config import get_config_from_spec
from minisweagent.models import get_model
from minisweagent.run.benchmarks.swebench import (
    get_sb_environment, update_preds_file, filter_instances, DATASET_MAPPING
)
from evoeval.orchestrator import evoeval_evaluate
from datasets import load_dataset

MAX_RETRIES = 2

def process_instance_with_evoeval(instance, output_dir, config):
    instance_id = instance["instance_id"]
    task = instance["problem_statement"]
    feedback_history = []

    for attempt in range(1, MAX_RETRIES + 2):  # up to 3 attempts
        # Build config (inject feedback on retries)
        run_config = deepcopy(config)
        if feedback_history:
            run_config["agent"]["instance_template"] += (
                "\n<prior_feedback>\n"
                + "\n---\n".join(feedback_history)
                + "\n</prior_feedback>"
            )

        # Run agent
        env = get_sb_environment(run_config, instance)
        model = get_model(config=run_config.get("model", {}))
        agent = DefaultAgent(model, env, **run_config.get("agent", {}))
        info = agent.run(task)
        patch_diff = info.get("submission", "")

        # Save trajectory
        agent.save(output_dir / instance_id / f"attempt_{attempt}.traj.json")

        # Run EvoEval verification (unless last attempt)
        if attempt <= MAX_RETRIES:
            decision, feedback = evoeval_evaluate(
                patch_diff=patch_diff,
                issue_text=task,
                env=env,  # for running checks in Docker
            )
            if decision == "accept":
                break
            feedback_history.append(feedback)
        # else: last attempt, submit regardless

    update_preds_file(output_dir / "preds.json", instance_id, model_name, patch_diff)
```

This shows exactly how mini-swe-agent's `DefaultAgent`, `get_sb_environment`, and `update_preds_file` plug into your EvoEval pipeline.

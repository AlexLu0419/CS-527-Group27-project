# Running mini-swe-agent on the 50-Instance Benchmark

This guide covers how to set up and run [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) on the 50-instance SWE-bench Verified subset defined in `data/instance_ids.json`.

## Prerequisites

- Python 3.10+
- Docker (required to run SWE-bench evaluation environments)

## Setup

Install [uv](https://docs.astral.sh/uv/) if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Create the environment and install all dependencies from `pyproject.toml`:

```bash
uv sync
```

To run a script without activating the environment:

```bash
uv run python -m sieve.reproduction.runner --help
```

## Environment Setup

### 1. Clone and install mini-swe-agent

```bash
git clone https://github.com/SWE-agent/mini-swe-agent mini-swe-agent
cd mini-swe-agent
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cd ..
```

### 2. Set your API key

```bash
export YOUR_API_KEY="your_key_here"
```

Or add it to mini-swe-agent's global config file at `~/.config/mini-swe-agent/.env`.

### 3. Verify Docker is running

```bash
docker info
```

## Running the Agent

Build the instance filter from `data/instance_ids.json` and run:

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
  --output <output_dir> \
  -m <your_model_choice> \
  -w 4
```

**Key flags:**

| Flag | Description |
|------|-------------|
| `--subset verified` | Loads `princeton-nlp/SWE-Bench_Verified` (500 instances) from HuggingFace |
| `--split test` | Uses the test split |
| `--filter` | Regex to select only the 50 instances in `instance_ids.json` |
| `--output` | Directory where trajectories and `preds.json` are saved |
| `-m` | Model name (via litellm, e.g. `gemini/gemini-2.5-pro`, `gemini/gemini-2.5-flash`) |
| `-w` | Number of parallel Docker workers. A larger number will consume credits at a higher per token rate. Using `2` is recommended |

## Resuming a Partial Run

If the run is interrupted (e.g. rate limit errors), check which instances need to be rerun:

```bash
python3 -c "
import json, yaml

all_ids = set(json.load(open('data/instance_ids.json'))['instance_ids'])
statuses = yaml.safe_load(open('<output_dir>/exit_statuses_*.yaml'))
submitted = set(statuses['instances_by_exit_status'].get('Submitted', []))
todo = all_ids - submitted
print('|'.join(sorted(todo)))
"
```

Then rerun with that filter and the same `--output` directory. Existing submitted instances will be skipped automatically (the script reads `preds.json` to skip already-completed instances).

## Evaluating Results

### Local evaluation (requires `swebench` package)

```bash
pip install swebench

python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --predictions_path <output_dir>/preds.json \
  --max_workers 4 \
  --run_id my-run
```

### Cloud evaluation (faster, no Docker needed)

```bash
pip install sb-cli

sb-cli submit swe-bench_verified test \
  --predictions_path <output_dir>/preds.json \
  --run_id my-run
```

Results are typically available within 20 minutes.

## Output Structure

```
<output_dir>/
├── preds.json                        # Model patches for all instances (input to evaluation)
├── minisweagent.log                  # Full run log
├── exit_statuses_<timestamp>.yaml    # Per-instance exit status summary
└── <instance_id>/
    └── <instance_id>.traj.json       # Full agent trajectory for the instance
```

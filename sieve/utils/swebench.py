"""Helpers for loading SWE-bench instances and creating Docker environments."""

import json
import logging
import re
from pathlib import Path

from datasets import load_dataset

logger = logging.getLogger("sieve.utils.swebench")

DATASET_MAPPING = {
    "full": "princeton-nlp/SWE-Bench",
    "verified": "princeton-nlp/SWE-Bench_Verified",
    "lite": "princeton-nlp/SWE-Bench_Lite",
}

def load_instances(
    subset: str = "verified",
    split: str = "test",
    instance_ids: list[str] | None = None,
) -> list[dict]:
    """Load SWE-bench instances, optionally filtering to specific IDs.

    Args:
        subset: Which SWE-bench subset ("verified", "lite", "full").
        split: Dataset split ("test", "dev").
        instance_ids: If provided, only return instances with these IDs.

    Returns:
        List of instance dicts with keys like instance_id, problem_statement, repo, etc.
    """
    dataset_path = DATASET_MAPPING.get(subset, subset)
    logger.info(f"Loading dataset {dataset_path}, split {split}...")
    instances = list(load_dataset(dataset_path, split=split))

    if instance_ids:
        id_set = set(instance_ids)
        instances = [inst for inst in instances if inst["instance_id"] in id_set]
        logger.info(f"Filtered to {len(instances)} instances")

    return instances


def load_instance_ids_from_file(path: str | Path) -> list[str]:
    """Load instance IDs from a JSON file (expects {"instance_ids": [...]})."""
    data = json.loads(Path(path).read_text())
    return data["instance_ids"]


def create_docker_env(instance: dict, timeout: int = 60) -> "DockerEnvironment":
    """Create a Docker environment for a SWE-bench instance.

    Uses mini-swe-agent's get_sb_environment with the SWE-bench config defaults.

    Args:
        instance: SWE-bench instance dict.
        timeout: Command execution timeout in seconds.

    Returns:
        A DockerEnvironment connected to the instance's container.
    """
    # Lazy import: avoids pulling in mini-swe-agent at module load time
    # (minisweagent transitively imports typer, which has Python 3.13 issues).
    from minisweagent.run.benchmarks.swebench import get_sb_environment  # noqa: PLC0415

    config = {
        "environment": {
            "environment_class": "docker",
            "cwd": "/testbed",
            "timeout": timeout,
            "interpreter": ["bash", "-lc"],
            "env": {
                "PAGER": "cat",
                "MANPAGER": "cat",
                "PIP_PROGRESS_BAR": "off",
                "TQDM_DISABLE": "1",
            },
        },
    }
    return get_sb_environment(config, instance)


def extract_repo_name(instance: dict) -> str:
    """Extract the human-readable repo name (e.g., 'django/django') from an instance."""
    return instance.get("repo", instance["instance_id"].rsplit("-", 1)[0].replace("__", "/"))

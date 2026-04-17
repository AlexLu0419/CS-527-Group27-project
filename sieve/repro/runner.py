"""Copy + run multiple Python scripts inside a Docker container."""
from __future__ import annotations

import logging
import subprocess

from sieve.phases._docker import copy_script_to_container, docker_exec_login

logger = logging.getLogger("sieve.repro.runner")


def run_scripts(
    container_id: str,
    scripts: dict[str, str],
    *,
    timeout: int = 60,
    workdir: str = "/testbed",
) -> dict[str, tuple[int, str]]:
    """Copy each script to ``/tmp/<cand_id>.py`` and run with ``python -I -B``.

    Returns ``{cand_id: (exit_code, stdout_tail)}``. On infra failure exit_code is -1.
    """
    results: dict[str, tuple[int, str]] = {}
    for cand_id, source in scripts.items():
        remote = f"/tmp/sieve_repro_{cand_id}.py"
        ok, err = copy_script_to_container(container_id, source, remote)
        if not ok:
            results[cand_id] = (-1, f"(copy failed: {err})")
            continue
        cmd = f"cd {workdir} && python -I -B {remote} 2>&1"
        try:
            r = docker_exec_login(container_id, cmd, timeout=timeout)
            output = r.stdout or ""
            results[cand_id] = (r.returncode, output[-2000:])
        except subprocess.TimeoutExpired:
            results[cand_id] = (-2, f"(timeout after {timeout}s)")
        except Exception as e:
            results[cand_id] = (-1, f"(exec failed: {e})")
    return results

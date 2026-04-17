"""Docker primitives shared across dynamic / reproduction / judge phases.

All helpers are private-by-underscore except ``docker_session``, which is
the public context manager callers should prefer so that container teardown
is guaranteed.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import tempfile

logger = logging.getLogger("sieve.phases._docker")


def image_name(instance_id: str) -> str:
    """Return the SWE-bench Docker image name for an instance."""
    transformed = instance_id.replace("__", "_1776_").lower()
    return f"swebench/sweb.eval.x86_64.{transformed}:latest"


def run_cmd(cmd: list[str], timeout: int = 120, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)


def start_container(img: str) -> str | None:
    try:
        r = run_cmd(
            ["docker", "run", "-d", "--rm", "--platform", "linux/amd64", img, "sleep", "2h"],
            timeout=180,
        )
        cid = r.stdout.strip()
        if r.returncode == 0 and cid:
            return cid
        logger.warning("start_container: failed for %s — %s", img, r.stderr.strip()[:200])
        return None
    except Exception as exc:
        logger.warning("start_container: exception — %s", exc)
        return None


def stop_container(cid: str) -> None:
    try:
        subprocess.run(["docker", "stop", cid], capture_output=True, timeout=30)
    except Exception:
        pass


def docker_exec_login(cid: str, shell_cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run *shell_cmd* via bash login shell (activates conda ``testbed`` env)."""
    return run_cmd(["docker", "exec", cid, "bash", "-lc", shell_cmd], timeout=timeout)


def docker_exec(cid: str, cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return run_cmd(["docker", "exec", cid, *cmd], timeout=timeout)


def copy_patch_to_container(cid: str, patch_content: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
        f.write(patch_content)
        local_path = f.name
    try:
        r = run_cmd(["docker", "cp", local_path, f"{cid}:/tmp/patch.diff"], timeout=30)
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, ""
    finally:
        os.unlink(local_path)


def copy_script_to_container(cid: str, script_content: str, remote_path: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script_content)
        local_path = f.name
    try:
        r = run_cmd(["docker", "cp", local_path, f"{cid}:{remote_path}"], timeout=30)
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, ""
    finally:
        os.unlink(local_path)


def apply_patch(cid: str) -> tuple[bool, str]:
    """Apply ``/tmp/patch.diff`` to ``/testbed``. Tries ``git apply`` then ``patch -p1``."""
    r = docker_exec(cid, ["git", "-C", "/testbed", "apply", "/tmp/patch.diff"])
    if r.returncode == 0:
        return True, ""
    r2 = docker_exec_login(cid, "cd /testbed && patch -p1 < /tmp/patch.diff")
    if r2.returncode == 0:
        return True, ""
    err = f"git apply: {r.stderr.strip()[:200]}\npatch: {r2.stderr.strip()[:200]}"
    return False, err


def parse_pytest_output(output: str) -> set[str]:
    """Extract failing test node IDs from pytest ``-v`` or ``-q`` output."""
    failures: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        m = re.match(r"^FAILED\s+(\S+)", line)
        if m:
            failures.add(m.group(1))
            continue
        m2 = re.match(r"^(\S+)\s+FAILED\s*$", line)
        if m2 and "::" in m2.group(1):
            failures.add(m2.group(1))
    return failures


@contextlib.contextmanager
def docker_session(instance_id: str):
    """Yield a running container ID; guarantee teardown on any exit path.

    Usage:
        with docker_session(inst_id) as cid:
            if cid is None:
                ... handle startup failure ...
            else:
                ... use cid ...
    """
    img = image_name(instance_id)
    cid = start_container(img)
    try:
        yield cid
    finally:
        if cid:
            stop_container(cid)

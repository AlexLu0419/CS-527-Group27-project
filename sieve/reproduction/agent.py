"""Reproduction Agent: generates and validates bug reproduction scripts.

Workflow:
  1. Call LLM with issue description only (no repo context) -> reproduction script
  2. Write the script into the SWE-bench Docker container
  3. Execute it on the unpatched (buggy) code
  4. Validate: the script must FAIL (non-zero exit) on unpatched code
  5. If it passes or errors out with an import/setup issue, refine with feedback (up to max_attempts)
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import litellm

from sieve.reproduction.prompts import (
    SYSTEM_PROMPT,
    GENERATE_USER_TEMPLATE,
    REFINE_USER_TEMPLATE,
)
from sieve.utils.swebench import create_docker_env, extract_repo_name

logger = logging.getLogger("sieve.reproduction")

REPRO_SCRIPT_PATH = "/tmp/repro_test.py"


@dataclass
class ReproductionResult:
    instance_id: str
    validated: bool
    repro_test_code: str | None = None
    attempts: int = 0
    execution_logs: list[dict] = field(default_factory=list)
    error: str | None = None
    total_cost: float = 0.0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class ReproductionAgent:
    """Generates a reproduction test for a SWE-bench issue and validates it."""

    def __init__(
        self,
        model_name: str = "gemini/gemini-2.5-flash",
        max_attempts: int = 3,
        execution_timeout: int = 60,
        model_kwargs: dict | None = None,
    ):
        self.model_name = model_name
        self.max_attempts = max_attempts
        self.execution_timeout = execution_timeout
        self.model_kwargs = model_kwargs or {"temperature": 0.0}

    def run(self, instance: dict) -> ReproductionResult:
        """Run the reproduction agent on a single SWE-bench instance.

        Args:
            instance: SWE-bench instance dict with at least
                      instance_id, problem_statement, and repo.

        Returns:
            ReproductionResult with the validated test or failure info.
        """
        instance_id = instance["instance_id"]
        result = ReproductionResult(instance_id=instance_id)
        start_time = time.time()

        repo = extract_repo_name(instance)
        title = instance.get("problem_statement", "").split("\n", 1)[0]
        problem_statement = instance["problem_statement"]

        # Start Docker container for this instance
        logger.info(f"[{instance_id}] Starting Docker environment...")
        env = None
        try:
            env = create_docker_env(instance, timeout=self.execution_timeout)
        except Exception as e:
            result.error = f"Failed to start Docker environment: {e}"
            result.elapsed_seconds = time.time() - start_time
            logger.error(f"[{instance_id}] {result.error}")
            return result

        try:
            script = None
            for attempt in range(1, self.max_attempts + 1):
                result.attempts = attempt
                logger.info(f"[{instance_id}] Attempt {attempt}/{self.max_attempts}")

                # Phase 1: Generate or refine the reproduction script
                if attempt == 1:
                    script = self._generate_script(repo, title, problem_statement)
                else:
                    last_log = result.execution_logs[-1]
                    script = self._refine_script(
                        repo,
                        title,
                        problem_statement,
                        previous_script=script,
                        exit_code=last_log["exit_code"],
                        execution_output=last_log["output"],
                    )

                result.repro_test_code = script
                result.total_cost = self._total_cost

                # Phase 2: Execute in Docker
                exec_result = self._execute_script(env, script)
                log_entry = {
                    "attempt": attempt,
                    "exit_code": exec_result["returncode"],
                    "output": exec_result["output"][:5000],  # truncate for storage
                }
                result.execution_logs.append(log_entry)

                # Phase 3: Validate — script must FAIL on unpatched code
                if exec_result["returncode"] != 0:
                    # Non-zero exit = script fails on buggy code = correct!
                    # But we should distinguish between "test assertion failed" (good)
                    # and "import error / syntax error" (bad — the script itself is broken)
                    if self._is_meaningful_failure(exec_result):
                        logger.info(
                            f"[{instance_id}] Validated on attempt {attempt} "
                            f"(exit code: {exec_result['returncode']})"
                        )
                        result.validated = True
                        break
                    else:
                        logger.info(
                            f"[{instance_id}] Script error on attempt {attempt} "
                            f"(likely broken script, will refine)"
                        )
                else:
                    # Exit code 0 = script passes on buggy code = wrong
                    logger.info(
                        f"[{instance_id}] Script passed on unpatched code "
                        f"(attempt {attempt}), will refine"
                    )
        finally:
            if env is not None:
                env.cleanup()

        result.elapsed_seconds = time.time() - start_time
        if not result.validated:
            logger.warning(
                f"[{instance_id}] Failed to generate valid reproduction test "
                f"after {result.attempts} attempts"
            )
        return result

    def _generate_script(self, repo: str, title: str, problem_statement: str) -> str:
        """Phase 1: Generate reproduction script from issue description only."""
        user_msg = GENERATE_USER_TEMPLATE.format(
            repo=repo,
            title=title,
            problem_statement=problem_statement,
        )
        return self._call_llm(user_msg)

    def _refine_script(
        self,
        repo: str,
        title: str,
        problem_statement: str,
        previous_script: str,
        exit_code: int,
        execution_output: str,
    ) -> str:
        """Phase 2b: Refine the script with execution feedback."""
        user_msg = REFINE_USER_TEMPLATE.format(
            repo=repo,
            title=title,
            problem_statement=problem_statement,
            previous_script=previous_script,
            exit_code=exit_code,
            execution_output=execution_output[:3000],  # truncate long outputs
        )
        return self._call_llm(user_msg)

    def _call_llm(self, user_message: str) -> str:
        """Call the LLM and return the generated script text."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        response = litellm.completion(
            model=self.model_name,
            messages=messages,
            **self.model_kwargs,
        )
        text = response.choices[0].message.content.strip()

        # Track cost
        try:
            cost = litellm.cost_calculator.completion_cost(response, model=self.model_name)
        except Exception:
            cost = 0.0
        self._total_cost = getattr(self, "_total_cost", 0.0) + cost

        # Strip markdown fences if the model wraps in ```python ... ```
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last fence lines
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        return text

    def _execute_script(self, env, script: str) -> dict:
        """Write the script to the container and execute it."""
        # Escape the script for shell heredoc
        escaped = script.replace("\\", "\\\\").replace("'", "'\\''")
        write_cmd = f"cat > {REPRO_SCRIPT_PATH} << 'REPRO_EOF'\n{script}\nREPRO_EOF"
        env.execute({"command": write_cmd})

        # Run the script
        run_cmd = f"cd /testbed && python {REPRO_SCRIPT_PATH} 2>&1"
        return env.execute({"command": run_cmd}, timeout=self.execution_timeout)

    def _is_meaningful_failure(self, exec_result: dict) -> bool:
        """Check if a non-zero exit is a meaningful test failure vs a broken script.

        A meaningful failure is an AssertionError, expected exception, or test logic failure.
        A broken script failure is an ImportError, SyntaxError, ModuleNotFoundError, etc.
        """
        output = exec_result.get("output", "")
        # These indicate the script itself is broken, not that it's testing the bug
        broken_indicators = [
            "SyntaxError:",
            "IndentationError:",
            "ModuleNotFoundError:",
            "ImportError:",
            "FileNotFoundError:",
            "NameError:",
        ]
        for indicator in broken_indicators:
            if indicator in output:
                return False
        return True


def save_results(results: list[ReproductionResult], output_path: str | Path):
    """Save reproduction results to a JSON file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = {r.instance_id: r.to_dict() for r in results}
    output_path.write_text(json.dumps(data, indent=2))
    logger.info(f"Saved {len(results)} results to {output_path}")

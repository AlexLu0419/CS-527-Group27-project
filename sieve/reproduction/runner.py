"""Batch runner for the Reproduction Agent over SWE-bench instances."""

import argparse
import concurrent.futures
import json
import logging
import sys
from pathlib import Path

from sieve.reproduction.agent import ReproductionAgent, ReproductionResult, save_results
from sieve.utils.swebench import load_instances, load_instance_ids_from_file

logger = logging.getLogger("sieve.reproduction.runner")


def run_single(agent: ReproductionAgent, instance: dict) -> ReproductionResult:
    """Run the reproduction agent on a single instance with error handling."""
    instance_id = instance["instance_id"]
    try:
        return agent.run(instance)
    except Exception as e:
        logger.error(f"[{instance_id}] Unhandled error: {e}", exc_info=True)
        return ReproductionResult(
            instance_id=instance_id,
            validated=False,
            error=f"Unhandled error: {e}",
        )


def run_batch(
    instances: list[dict],
    model_name: str = "gemini/gemini-3-flash-preview",
    max_attempts: int = 3,
    execution_timeout: int = 60,
    workers: int = 1,
    output_path: str | Path = "reproduction_results.json",
    resume: bool = True,
) -> list[ReproductionResult]:
    """Run the reproduction agent on a batch of instances.

    Args:
        instances: List of SWE-bench instance dicts.
        model_name: LLM model name (via litellm).
        max_attempts: Max attempts per instance to generate a valid repro test.
        execution_timeout: Timeout in seconds for executing the repro script.
        workers: Number of parallel workers.
        output_path: Path to save results JSON.
        resume: If True, skip instances that already have results in output_path.

    Returns:
        List of ReproductionResult objects.
    """
    output_path = Path(output_path)

    # Resume: load existing results and skip completed instances
    existing_results = {}
    if resume and output_path.exists():
        existing_results = json.loads(output_path.read_text())
        completed_ids = set(existing_results.keys())
        before = len(instances)
        instances = [i for i in instances if i["instance_id"] not in completed_ids]
        logger.info(f"Resuming: skipping {before - len(instances)} already-completed instances")

    if not instances:
        logger.info("All instances already completed.")
        return [ReproductionResult(**v) for v in existing_results.values()]

    logger.info(f"Running reproduction agent on {len(instances)} instances with {workers} workers")

    results: list[ReproductionResult] = []

    if workers <= 1:
        # Sequential execution
        for i, instance in enumerate(instances):
            logger.info(f"[{i+1}/{len(instances)}] Processing {instance['instance_id']}")
            agent = ReproductionAgent(
                model_name=model_name,
                max_attempts=max_attempts,
                execution_timeout=execution_timeout,
            )
            result = run_single(agent, instance)
            results.append(result)
            # Save incrementally
            _save_incremental(output_path, existing_results, results)
            _log_progress(results, i + 1, len(instances))
    else:
        # Parallel execution
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_instance = {}
            for instance in instances:
                agent = ReproductionAgent(
                    model_name=model_name,
                    max_attempts=max_attempts,
                    execution_timeout=execution_timeout,
                )
                future = executor.submit(run_single, agent, instance)
                future_to_instance[future] = instance["instance_id"]

            for i, future in enumerate(concurrent.futures.as_completed(future_to_instance)):
                instance_id = future_to_instance[future]
                try:
                    result = future.result()
                except Exception as e:
                    logger.error(f"[{instance_id}] Future error: {e}")
                    result = ReproductionResult(
                        instance_id=instance_id,
                        validated=False,
                        error=str(e),
                    )
                results.append(result)
                _save_incremental(output_path, existing_results, results)
                _log_progress(results, i + 1, len(instances))

    # Final save
    _save_incremental(output_path, existing_results, results)
    _log_summary(results)
    return results


def _save_incremental(
    output_path: Path,
    existing_results: dict,
    new_results: list[ReproductionResult],
):
    """Save all results (existing + new) to disk."""
    combined = dict(existing_results)
    for r in new_results:
        combined[r.instance_id] = r.to_dict()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(combined, indent=2))


def _log_progress(results: list[ReproductionResult], done: int, total: int):
    validated = sum(1 for r in results if r.validated)
    logger.info(f"Progress: {done}/{total} done, {validated}/{done} validated")


def _log_summary(results: list[ReproductionResult]):
    total = len(results)
    validated = sum(1 for r in results if r.validated)
    errors = sum(1 for r in results if r.error)
    total_cost = sum(r.total_cost for r in results)
    avg_attempts = sum(r.attempts for r in results) / max(total, 1)

    logger.info("=" * 60)
    logger.info(f"Reproduction Agent Summary")
    logger.info(f"  Total instances:     {total}")
    logger.info(f"  Validated:           {validated} ({validated/max(total,1)*100:.1f}%)")
    logger.info(f"  Errors:              {errors}")
    logger.info(f"  Avg attempts:        {avg_attempts:.1f}")
    logger.info(f"  Total LLM cost:      ${total_cost:.4f}")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Run SIEVE Reproduction Agent on SWE-bench instances")
    parser.add_argument(
        "--instance-ids",
        type=str,
        default="data/instance_ids.json",
        help="Path to JSON file with instance IDs",
    )
    parser.add_argument(
        "--subset",
        type=str,
        default="verified",
        help="SWE-bench subset (verified, lite, full)",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="Dataset split",
    )
    parser.add_argument(
        "-m", "--model",
        type=str,
        default="gemini/gemini-3-flash-preview",
        help="LLM model name (via litellm)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Max attempts per instance",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Execution timeout in seconds",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=1,
        help="Number of parallel workers",
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default="runs/reproduction/results.json",
        help="Output path for results JSON",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Don't skip already-completed instances",
    )
    parser.add_argument(
        "--filter",
        type=str,
        default="",
        help="Regex filter for instance IDs",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Load instance IDs
    instance_ids = load_instance_ids_from_file(args.instance_ids)
    instances = load_instances(
        subset=args.subset,
        split=args.split,
        instance_ids=instance_ids,
    )

    # Apply regex filter if provided
    if args.filter:
        import re
        instances = [i for i in instances if re.match(args.filter, i["instance_id"])]
        logger.info(f"Filter matched {len(instances)} instances")

    run_batch(
        instances=instances,
        model_name=args.model,
        max_attempts=args.max_attempts,
        execution_timeout=args.timeout,
        workers=args.workers,
        output_path=args.output,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""run_phase_a_only.py — Phase-A-only pilot driver for v7 gating.

Regenerates Phase A (generate + gate) for every instance in preds.json
and records per-instance bucket counts + drop reasons. Skips Phase B,
cascade, and retry entirely — cheap fail-fast validation of the
framework-aware generation hints before committing full cascade budget.

The phase_a() call writes to runs/phase_a_cache/{iid}.json. This script
just drives the iteration and aggregates a concise summary at the end.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sieve.repro.cache import PHASE_A_SCHEMA_VERSION, phase_a
from sieve.utils.swebench import load_instances

DEFAULT_PREDS = REPO_ROOT / "runs/swe-verified_50_gpt5-mini/preds.json"
DEFAULT_SUMMARY = REPO_ROOT / "runs/phase_a_pilot_v7_summary.json"


def _summarize_cache(iid: str, cache) -> dict:
    buckets: Counter = Counter()
    drops: Counter = Counter()
    for g in cache.gated_tests:
        b = g.get("bucket", "?")
        buckets[b] += 1
        if b == "DROP":
            drops[g.get("drop_reason") or "unknown"] += 1
    return {
        "instance_id": iid,
        "schema_version": cache.schema_version,
        "zero_signal": cache.zero_signal,
        "n_candidates": len(cache.candidates),
        "n_surviving": len(cache.surviving()),
        "buckets": dict(buckets),
        "drop_reasons": dict(drops),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--preds", type=Path, default=DEFAULT_PREDS)
    p.add_argument("--summary-out", type=Path, default=DEFAULT_SUMMARY)
    p.add_argument("--only", type=str, default=None,
                   help="Comma-separated instance_ids (for quick iteration)")
    args = p.parse_args()

    preds = json.loads(args.preds.read_text())
    ids = sorted(preds.keys())
    if args.only:
        selected = {s.strip() for s in args.only.split(",") if s.strip()}
        ids = [iid for iid in ids if iid in selected]

    print(f"Schema version         : {PHASE_A_SCHEMA_VERSION}")
    print(f"Phase-A pilot run over : {len(ids)} instances")
    print()

    instances_list = load_instances(subset="verified", instance_ids=ids)
    instances_by_id = {inst["instance_id"]: inst for inst in instances_list}

    rows: list[dict] = []
    for i, iid in enumerate(ids, 1):
        inst = instances_by_id.get(iid)
        if inst is None:
            print(f"[{i}/{len(ids)}] {iid}  MISSING from dataset")
            rows.append({"instance_id": iid, "error": "not_in_dataset"})
            continue

        t0 = time.time()
        try:
            cache = phase_a(inst)
        except Exception as exc:
            print(f"[{i}/{len(ids)}] {iid}  EXCEPTION: {exc}")
            rows.append({"instance_id": iid, "error": str(exc)[:300]})
            continue
        dt = time.time() - t0
        row = _summarize_cache(iid, cache)
        row["elapsed_sec"] = round(dt, 1)
        rows.append(row)
        print(
            f"[{i}/{len(ids)}] {iid:50s} "
            f"surv={row['n_surviving']:>1d}/{row['n_candidates']:>1d} "
            f"A={row['buckets'].get('A', 0)} B={row['buckets'].get('B', 0)} "
            f"C={row['buckets'].get('C', 0)} DROP={row['buckets'].get('DROP', 0)} "
            f"zs={row['zero_signal']}  {dt:.1f}s"
        )

    # Aggregate
    n = len(rows)
    n_a = sum(1 for r in rows if r.get("buckets", {}).get("A", 0) > 0)
    n_any = sum(1 for r in rows if r.get("n_surviving", 0) > 0)
    n_zs = sum(1 for r in rows if r.get("zero_signal"))
    drop_tally: Counter = Counter()
    for r in rows:
        for k, v in (r.get("drop_reasons") or {}).items():
            drop_tally[k] += v

    summary = {
        "schema_version": PHASE_A_SCHEMA_VERSION,
        "n_total": n,
        "n_with_bucket_A": n_a,
        "n_with_any_surviving": n_any,
        "n_zero_signal": n_zs,
        "drop_reason_totals": dict(drop_tally),
        "rows": rows,
    }
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print()
    print(f"=== Phase-A pilot summary (schema {PHASE_A_SCHEMA_VERSION}) ===")
    print(f"Instances with any bucket-A test : {n_a}/{n}")
    print(f"Instances with any surviving test: {n_any}/{n}")
    print(f"Zero-signal instances            : {n_zs}/{n}")
    print(f"Top drop reasons                 : {drop_tally.most_common(5)}")
    print(f"\nSummary written to: {args.summary_out}")


if __name__ == "__main__":
    main()

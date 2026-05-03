#!/usr/bin/env python3
"""merge_retry_preds.py — fallback-on-empty merge for retry-round preds.

Layers (the retry layer only replaces an entry when its ``model_patch`` is
non-empty, so empty / failed retry preds fall back to the first-run patch):

  1. ``--first-run``      base (full 50-instance preds)
  2. ``--retry``          cascade-rejected roster output

Output: a full 50-entry predictions file the SWE-bench harness can evaluate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _overlay(merged: dict, source: dict, label: str) -> tuple[int, int, list[str]]:
    """Replace entries in ``merged`` from ``source`` when source has a non-empty
    patch. Returns (replaced_count, empty_count, replaced_ids)."""
    replaced = 0
    empty = 0
    replaced_ids: list[str] = []
    for iid, rec in source.items():
        patch = (rec or {}).get("model_patch", "") or ""
        if patch.strip():
            merged[iid] = rec
            replaced += 1
            replaced_ids.append(iid)
        else:
            empty += 1
    print(f"{label:<24}: replaced={replaced} empty={empty} total={len(source)}")
    return replaced, empty, replaced_ids


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--first-run", type=Path, required=True,
                   help="First-run preds.json (50-instance base).")
    p.add_argument("--retry", type=Path, required=True,
                   help="Retry preds.json (cascade-rejected roster output).")
    p.add_argument("--out", type=Path, required=True,
                   help="Destination merged preds.json.")
    args = p.parse_args()

    base = json.loads(args.first_run.read_text())
    retry = json.loads(args.retry.read_text())

    merged = dict(base)
    print(f"{'first_run':<24}: {len(base)} entries (base)")
    _, _, retry_ids = _overlay(merged, retry, "retry")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(merged, indent=2))

    print(f"merged total              : {len(merged)}")
    print(f"retry-replaced ids        : {retry_ids}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

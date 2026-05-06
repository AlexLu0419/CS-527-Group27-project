#!/usr/bin/env python3
"""cascade_matrix.py

Given the four SIEVE cascade result JSONs (static, dynamic_regression,
reproduction, judge) and the SWE-bench ground-truth report, print the
TP/FP/TN/FN confusion matrix for the final cascade verdict.

Definitions (from docs/reproduction_precision_report.md §0):
    TP = cascade FAIL on unresolved patch (correct catch)
    FP = cascade FAIL on resolved patch   (wrongly blocked good patch)
    TN = cascade PASS on resolved patch   (correctly let through good patch)
    FN = cascade PASS on unresolved patch (wrongly let through bad patch)
    UNCERTAIN = not counted in confusion matrix
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from build_retry_manifest import _cascade_final


def _index(path: Path) -> dict:
    if not path.exists():
        return {}
    return {r["instance_id"]: r for r in json.loads(path.read_text())}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--static", type=Path, required=True)
    p.add_argument("--dynreg", type=Path, required=True)
    p.add_argument("--repro", type=Path, required=True)
    p.add_argument("--judge", type=Path, required=True)
    p.add_argument("--gt", type=Path, required=True,
                   help="SWE-bench ground-truth report (resolved_ids/unresolved_ids)")
    p.add_argument("--label", type=str, default="cascade",
                   help="Label to print above the matrix")
    args = p.parse_args()

    static = _index(args.static)
    dynreg = _index(args.dynreg)
    repro = _index(args.repro)
    judge = _index(args.judge)

    gt = json.loads(args.gt.read_text())
    resolved = set(gt.get("resolved_ids", []))
    unresolved = set(gt.get("unresolved_ids", []))

    # Canonical roster = repro (always run on every instance)
    ids = sorted(repro.keys()) or sorted(static.keys())

    rows: list[tuple[str, str, str, str, str]] = []  # (iid, final, layer, gt, cls)
    verdict_counts: Counter[str] = Counter()
    layer_counts: Counter[str] = Counter()
    cls_counts: Counter[str] = Counter()
    mistakes: list[tuple[str, str, str, str]] = []  # (iid, gt, final, layer)

    for iid in ids:
        final, layer = _cascade_final(
            iid, static=static, dynreg=dynreg, repro=repro, judge=judge,
        )
        gt_label = (
            "resolved" if iid in resolved
            else "unresolved" if iid in unresolved
            else "unknown"
        )
        cls = "-"
        if final == "PASS":
            cls = "TN" if gt_label == "resolved" else ("FN" if gt_label == "unresolved" else "-")
        elif final == "FAIL":
            cls = "FP" if gt_label == "resolved" else ("TP" if gt_label == "unresolved" else "-")
        elif final == "UNCERTAIN":
            cls = "UNC"
        else:
            cls = final  # ERROR / MISSING / etc
        verdict_counts[final] += 1
        layer_counts[layer or "-"] += 1
        cls_counts[cls] += 1
        rows.append((iid, final, layer, gt_label, cls))
        if cls in ("FP", "FN"):
            mistakes.append((iid, gt_label, final, layer))

    tp = cls_counts.get("TP", 0)
    fp = cls_counts.get("FP", 0)
    tn = cls_counts.get("TN", 0)
    fn = cls_counts.get("FN", 0)
    unc = cls_counts.get("UNC", 0)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0

    print(f"=== {args.label} ===")
    print(f"Verdicts     : {dict(verdict_counts)}")
    print(f"Layers       : {dict(layer_counts)}")
    print(f"Confusion    : TP={tp} FP={fp} TN={tn} FN={fn} UNC={unc}")
    print(f"Precision    : {precision * 100:.1f}% ({tp}/{tp+fp})")
    print(f"Recall       : {recall * 100:.1f}% ({tp}/{tp+fn})")

    if mistakes:
        print("\nMistakes (FP + FN):")
        for iid, gt_label, final, layer in sorted(mistakes, key=lambda x: (x[1], x[0])):
            print(f"  {gt_label:10s}  {final:4s}  (layer={layer}) {iid}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()

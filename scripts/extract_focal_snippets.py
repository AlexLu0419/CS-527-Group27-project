#!/usr/bin/env python3
"""extract_focal_snippets.py — cache focal-file source snippets per instance.

For each retry-roster instance in the v10 SIEVE run, this script spins up
the SWE-bench Docker image, cats the focal file, extracts ~30 lines around
the focal symbol, and writes the snippet to
`runs/focal_snippets_cache/{iid}.json`. The retry-manifest builder then
reads these snippets without needing Docker.

Data flow
---------
Phase-A cache -> focal_file + focal_symbol -> extractor -> snippet cache

The focal_file / focal_symbol are LLM-derived; they may be wrong. We embed
the extracted snippet anyway and let the retry agent judge.

Usage
-----
    python scripts/extract_focal_snippets.py --filter runs/retry_gpt5mini_v9_1/retry_filter.txt
    python scripts/extract_focal_snippets.py --only sphinx-doc__sphinx-10673,django__django-11964
    python scripts/extract_focal_snippets.py  # all 50
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sieve.phases._docker import image_name

DEFAULT_PREDS = REPO_ROOT / "runs/swe-verified_50_gpt5-mini/preds.json"
DEFAULT_PHASE_A = REPO_ROOT / "runs/phase_a_cache"
DEFAULT_OUT = REPO_ROOT / "runs/focal_snippets_cache"

SNIPPET_CONTEXT_LINES = 15     # lines before and after the focal symbol
MAX_FILE_BYTES = 400_000       # skip giant files
MAX_SNIPPET_CHARS = 3000
MAX_SNIPPETS_PER_INSTANCE = 3  # v11 A: up to 3 focal snippets per instance
MAX_SYMBOL_MAP_ENTRIES = 40    # v11 B: cap symbols in AST map


def _is_test_path(p: str) -> bool:
    low = (p or "").lower().replace("\\", "/")
    if not low:
        return False
    if "/tests/" in low or low.startswith("tests/") or low.startswith("test/") or "/test/" in low:
        return True
    base = low.rsplit("/", 1)[-1]
    return base.startswith("test_") or base.endswith("_test.py") or base == "tests.py"


def _cat_file(image: str, path: str) -> str | None:
    """Run a one-shot container that cats the file, return contents or None."""
    try:
        r = subprocess.run(
            ["docker", "run", "--rm", "--platform", "linux/amd64", image,
             "cat", f"/testbed/{path}"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        print(f"    docker run failed: {exc}", file=sys.stderr)
        return None
    if r.returncode != 0:
        return None
    contents = r.stdout
    if len(contents.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
        return None
    return contents


def _find_symbol_line(text: str, symbol: str) -> int | None:
    """Return the 1-indexed line where the focal symbol lives.

    For `Class.method` symbols, prefer the method definition if present,
    else fall back to the `class` line. This handles cases where the
    symbol refers to a method that's inherited or needs to be added
    (common in bug reports: "Class.method should return X").
    """
    if not text or not symbol:
        return None
    parts = [p for p in symbol.split(".") if p]
    if not parts:
        return None
    tail = parts[-1]
    class_part = parts[-2] if len(parts) >= 2 else None

    lines = text.splitlines()

    def _scan(name: str, kinds: tuple[str, ...]) -> int | None:
        pats = []
        if "def" in kinds:
            pats += [
                re.compile(rf"^\s*def\s+{re.escape(name)}\s*\("),
                re.compile(rf"^\s*async\s+def\s+{re.escape(name)}\s*\("),
            ]
        if "class" in kinds:
            pats.append(re.compile(rf"^\s*class\s+{re.escape(name)}\s*[\(\:]"))
        if "var" in kinds:
            pats.append(re.compile(rf"^\s*{re.escape(name)}\s*="))
        for i, line in enumerate(lines, start=1):
            for pat in pats:
                if pat.match(line):
                    return i
        return None

    # Try the method/function name first (most specific).
    line = _scan(tail, ("def", "class", "var"))
    if line is not None:
        return line
    # Fall back to the containing class if the symbol is dotted.
    if class_part:
        line = _scan(class_part, ("class",))
        if line is not None:
            return line
    return None


def _extract_snippet(
    text: str, symbol: str, context_lines: int = SNIPPET_CONTEXT_LINES,
) -> tuple[str, int, int] | None:
    """Extract a snippet around the symbol. Returns (snippet, start_line, end_line)."""
    line_no = _find_symbol_line(text, symbol)
    if line_no is None:
        return None
    lines = text.splitlines()
    start = max(0, line_no - 1 - context_lines)
    end = min(len(lines), line_no + context_lines)
    snippet = "\n".join(lines[start:end])
    if len(snippet) > MAX_SNIPPET_CHARS:
        snippet = snippet[:MAX_SNIPPET_CHARS] + "\n# ... [truncated]"
    return snippet, start + 1, end


def _symbol_map(text: str) -> list[dict]:
    """v11 B: parse file via AST and return a compact list of top-level symbols.

    Returned shape:
      [{"kind": "class"|"func", "name": str, "line": int, "children": [{"kind","name","line"}...]}]

    Children are only populated for classes (methods) and top-level only.
    Gracefully returns [] on parse errors.
    """
    try:
        tree = ast.parse(text)
    except Exception:
        return []
    out: list[dict] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            children = []
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    children.append({
                        "kind": "func",
                        "name": sub.name,
                        "line": sub.lineno,
                    })
                    if len(children) >= 10:
                        break
            out.append({"kind": "class", "name": node.name,
                        "line": node.lineno, "children": children})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append({"kind": "func", "name": node.name,
                        "line": node.lineno, "children": []})
        if len(out) >= MAX_SYMBOL_MAP_ENTRIES:
            break
    return out


def _import_path_to_path(import_path: str) -> str | None:
    """'pkg.sub.module.Class' → 'pkg/sub/module.py'. Returns None on failure."""
    if not import_path:
        return None
    parts = [p for p in import_path.split(".") if p and p[:1].islower()]
    if not parts:
        return None
    return "/".join(parts) + ".py"


def _module_to_path(mod: str) -> str | None:
    """'pkg.sub.mod' → 'pkg/sub/mod.py'. Skip non-identifier-looking names."""
    if not mod or not re.match(r"^[a-zA-Z_][\w.]*$", mod):
        return None
    return mod.replace(".", "/") + ".py"


def _candidate_paths(cache: dict) -> list[str]:
    """Enumerate plausible focal source paths from Phase-A data.

    Priority:
      1. focal_files filtered for non-test
      2. import_path -> path
      3. affected_modules -> paths (filter noise like .rst references)

    All deduped, preserving order.
    """
    loc = cache.get("localization") or {}
    out: list[str] = []
    seen: set[str] = set()

    def _push(p: str | None):
        if p and p not in seen and not _is_test_path(p):
            seen.add(p)
            out.append(p)

    focal_files = loc.get("focal_files") or ([loc.get("focal_file")] if loc.get("focal_file") else [])
    for f in focal_files:
        _push(f)

    _push(_import_path_to_path(loc.get("import_path") or ""))

    issue = cache.get("issue") or {}
    for mod in (issue.get("affected_modules") or []):
        _push(_module_to_path(mod))
    return out


def process_instance(
    iid: str, phase_a_dir: Path, out_dir: Path, *, force: bool = False,
) -> dict:
    """v11: collect up to MAX_SNIPPETS_PER_INSTANCE snippets + symbol maps.

    Emits a new-format record:
        {"instance_id", "status", "focal_symbol",
         "entries": [
             {"focal_file", "snippet", "start_line", "end_line", "status",
              "symbol_map": [...]}
         ]}

    Backward-compatibility: the top-level fields of the *first* entry are
    also mirrored onto the record so older manifest readers that look at
    record['focal_file'] / ['snippet'] keep working.
    """
    out_path = out_dir / f"{iid}.json"
    if out_path.exists() and not force:
        return json.loads(out_path.read_text())

    cache_path = phase_a_dir / f"{iid}.json"
    if not cache_path.exists():
        return {"instance_id": iid, "status": "no_phase_a_cache"}
    cache = json.loads(cache_path.read_text())
    loc = cache.get("localization") or {}
    focal_symbol = loc.get("focal_symbol") or ""
    candidates = _candidate_paths(cache)
    if not candidates:
        return {"instance_id": iid, "status": "no_focal_file"}

    img = image_name(iid)
    entries: list[dict] = []
    fallback_entry: dict | None = None

    for focal in candidates:
        if len(entries) >= MAX_SNIPPETS_PER_INSTANCE:
            break
        contents = _cat_file(img, focal)
        if contents is None:
            continue
        sym_map = _symbol_map(contents)
        out = _extract_snippet(contents, focal_symbol)
        if out is not None:
            snippet, s, e = out
            entries.append({
                "focal_file": focal,
                "focal_symbol": focal_symbol,
                "snippet": snippet,
                "start_line": s,
                "end_line": e,
                "status": "ok",
                "symbol_map": sym_map,
            })
        elif fallback_entry is None:
            lines = contents.splitlines()
            fallback_entry = {
                "focal_file": focal,
                "focal_symbol": focal_symbol,
                "snippet": "\n".join(lines[: 2 * SNIPPET_CONTEXT_LINES]),
                "start_line": 1,
                "end_line": min(len(lines), 2 * SNIPPET_CONTEXT_LINES),
                "status": "snippet_symbol_not_found",
                "symbol_map": sym_map,
            }

    if not entries and fallback_entry is not None:
        entries = [fallback_entry]

    if not entries:
        return {"instance_id": iid, "status": "no_file_readable", "candidates": candidates}

    primary = entries[0]
    record = {
        "instance_id": iid,
        "status": primary["status"],
        "focal_symbol": focal_symbol,
        "entries": entries,
        # Backward-compat mirrors (for v10-era manifest readers)
        "focal_file": primary["focal_file"],
        "snippet": primary["snippet"],
        "start_line": primary["start_line"],
        "end_line": primary["end_line"],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2))
    return record


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--preds", type=Path, default=DEFAULT_PREDS)
    p.add_argument("--phase-a", type=Path, default=DEFAULT_PHASE_A)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--only", type=str, default=None,
                   help="Comma-separated instance_ids")
    p.add_argument("--filter", type=Path, default=None,
                   help="Path to a retry_filter.txt regex to extract instance IDs")
    p.add_argument("--force", action="store_true",
                   help="Re-extract even if cache entry exists")
    args = p.parse_args()

    preds = json.loads(args.preds.read_text())
    if args.only:
        ids = [s.strip() for s in args.only.split(",") if s.strip()]
    elif args.filter and args.filter.exists():
        pat = args.filter.read_text().strip()
        # Extract iids from anchored regex "^(?:iid1|iid2|...)$"
        inner = pat.strip("^$()").replace("?:", "")
        ids = sorted({re.sub(r"\\", "", s) for s in inner.split("|") if s})
    else:
        ids = sorted(preds.keys())

    print(f"Processing {len(ids)} instances")
    stats = {"ok": 0, "snippet_symbol_not_found": 0, "no_focal_file": 0,
             "no_phase_a_cache": 0, "no_file_readable": 0}
    for i, iid in enumerate(ids, 1):
        rec = process_instance(iid, args.phase_a, args.out_dir, force=args.force)
        s = rec.get("status", "?")
        stats[s] = stats.get(s, 0) + 1
        print(f"[{i}/{len(ids)}] {iid:<55s} status={s}")

    print()
    print("=== Summary ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

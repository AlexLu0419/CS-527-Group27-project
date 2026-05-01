"""Otter-style two-call localization with min-edit-distance filename repair.

Call 1: pick a test file (or "new file" + target directory)
Call 2: pick the focal function/class whose behavior the test should exercise.

Both calls' outputs get repaired against ground-truth listings from the
container to eliminate hallucinated file paths / symbol names.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from rapidfuzz import fuzz, process

from sieve.llm import complete
from sieve.phases._docker import docker_exec, docker_exec_login, docker_session
from sieve.prompts import load_prompt
from sieve.repro.issue import IssueBundle
from sieve.repro.skeleton import build_file_skeleton, list_symbols

logger = logging.getLogger("sieve.repro.localize")

_REPAIR_THRESHOLD = 85
_TEST_DIR_LISTING_MAX = 120  # lines
_MAX_FOCAL_FILES = 3         # top-k focal files for richer LLM context
_MAX_SYMBOL_GREP_HITS = 3    # v12 A: cap files promoted by symbol search


def _is_test_path(p: str) -> bool:
    """Heuristic: path looks like a test file (v12 C)."""
    low = (p or "").lower().replace("\\", "/")
    if not low:
        return False
    if "/tests/" in low or low.startswith("tests/") or low.startswith("test/") or "/test/" in low:
        return True
    base = low.rsplit("/", 1)[-1]
    return base.startswith("test_") or base.endswith("_test.py") or base == "tests.py"


def _symbol_name_parts(symbol: str) -> tuple[str, str | None]:
    """Parse a dotted symbol like 'TextChoices.__str__' into (leaf, container).

    Returns (leaf_name, container_name_or_None) where container is the part
    immediately preceding the leaf when the symbol is dotted.
    """
    parts = [p for p in (symbol or "").split(".") if p]
    if not parts:
        return "", None
    leaf = parts[-1]
    container = parts[-2] if len(parts) >= 2 else None
    return leaf, container


def _symbol_in_known(symbol: str, known: list[str]) -> bool:
    """Does `symbol` appear in the skeleton's known_symbols list?

    Accepts:
      - exact match
      - container match for dotted symbols (e.g. 'TextChoices.__str__' counts
        as present when 'TextChoices' is a known class).
    """
    if not symbol or not known:
        return False
    if symbol in known:
        return True
    leaf, container = _symbol_name_parts(symbol)
    if leaf and any(k == leaf or k.endswith("." + leaf) for k in known):
        return True
    if container and any(k == container or k.endswith("." + container) for k in known):
        return True
    return False


def _grep_symbol_location(
    cid: str, symbol: str, all_files: list[str], *, timeout: int = 30,
) -> list[str]:
    """Repo-wide grep for the symbol's definition site.

    v12 A strategy (tiered):
      1. For dotted symbols (``Class.method``), grep first for the **class**
         name only — the class's module is the strongest signal. Method
         names like ``__str__`` / ``resolve`` / ``process`` match hundreds
         of unrelated files and would be noise.
      2. If tier-1 yields no matches, fall back to grepping the leaf
         name (``def leaf``). This is the standalone-function case.

    Filters out test paths; caps at ``_MAX_SYMBOL_GREP_HITS``.
    """
    leaf, container = _symbol_name_parts(symbol)
    if not leaf:
        return []

    def _run(pattern: str) -> list[str]:
        cmd = (
            rf"grep -rln --include='*.py' -E '{pattern}' /testbed"
        )
        try:
            r = docker_exec_login(cid, cmd, timeout=timeout)
        except Exception:
            return []
        if r.returncode not in (0, 1):
            return []
        out: list[str] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("/testbed/"):
                line = line[len("/testbed/"):]
            if line in all_files and not _is_test_path(line) and line not in out:
                out.append(line)
        return out

    # Tier 1: class-definition search (for dotted symbols only)
    if container:
        hits = _run(rf"^[[:space:]]*class[[:space:]]+{container}[[:space:]]*[\(:]")
        if hits:
            return hits[:_MAX_SYMBOL_GREP_HITS]

    # Tier 2: leaf-name definition (standalone function / top-level class)
    hits = _run(
        rf"^[[:space:]]*(class|(async[[:space:]]+)?def)[[:space:]]+{leaf}[[:space:]]*[\(:]"
    )
    # For very common leaves (short names), likely too many hits; drop if >20.
    if len(hits) > 20:
        return []
    return hits[:_MAX_SYMBOL_GREP_HITS]


@dataclass
class Localization:
    test_file: str | None = None
    test_is_new: bool = False
    test_confidence: str = "low"
    focal_file: str | None = None           # primary (= focal_files[0] when present)
    focal_files: list[str] = field(default_factory=list)  # up to 3, primary first
    focal_symbol: str | None = None
    focal_kind: str | None = None  # function|method|class
    import_path: str | None = None
    confidence: float = 0.0
    repaired_filenames: list[str] = field(default_factory=list)
    repair_details: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _collect_repo_index(cid: str) -> tuple[list[str], list[str]]:
    """Return (all_py_files, test_py_files) relative to /testbed."""
    r = docker_exec(cid, ["git", "-C", "/testbed", "ls-files", "*.py"], timeout=30)
    files = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    tests = [f for f in files if ("/tests/" in f or f.startswith("tests/") or "/test_" in f or f.split("/")[-1].startswith("test_"))]
    return files, tests


def _fetch_file_source(cid: str, path: str) -> str:
    r = docker_exec_login(cid, f"cat /testbed/{path}", timeout=30)
    return r.stdout if r.returncode == 0 else ""


def _repair(name: str, candidates: list[str]) -> tuple[str, bool, int]:
    """Return (repaired_name, was_repaired, score)."""
    if not candidates or not name:
        return name, False, 0
    if name in candidates:
        return name, False, 100
    best = process.extractOne(name, candidates, scorer=fuzz.partial_ratio)
    if not best:
        return name, False, 0
    repaired, score, _idx = best
    if score >= _REPAIR_THRESHOLD:
        return repaired, True, score
    return name, False, score


def _compact_test_listing(test_files: list[str]) -> str:
    lines = test_files[:_TEST_DIR_LISTING_MAX]
    suffix = f"\n... [+{len(test_files) - _TEST_DIR_LISTING_MAX} more]" if len(test_files) > _TEST_DIR_LISTING_MAX else ""
    return "\n".join(lines) + suffix


def _path_to_import(path: str) -> str:
    """Best-effort convert 'a/b/c.py' → 'a.b.c'. Strip leading 'src/' if present."""
    p = path
    if p.startswith("src/"):
        p = p[4:]
    if p.endswith("/__init__.py"):
        p = p[: -len("/__init__.py")]
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def localize(issue: IssueBundle, *, cid: str | None = None) -> Localization:
    """Two-call localization. Opens a short-lived container if ``cid`` is not given."""
    if cid is not None:
        return _localize_with_cid(issue, cid)
    with docker_session(issue.instance_id) as c:
        if c is None:
            return Localization(error="container_startup_failed")
        return _localize_with_cid(issue, c)


def _localize_with_cid(issue: IssueBundle, cid: str) -> Localization:
    loc = Localization()
    try:
        all_files, test_files = _collect_repo_index(cid)
    except Exception as e:
        loc.error = f"ls-files_failed: {e}"
        return loc
    if not all_files:
        loc.error = "empty_repo_index"
        return loc

    # ----- Call 1: test file -----
    tfile_spec = load_prompt("localize_test_file")
    ctx = dict(
        one_line_summary=issue.one_line_summary or "(unknown)",
        affected_modules=issue.affected_modules,
        exception_type=issue.exception_type,
        test_dir_listing=_compact_test_listing(test_files or all_files),
    )
    resp = complete(
        role=tfile_spec.role,
        system=tfile_spec.system,
        messages=tfile_spec.messages(**ctx),
        temperature=tfile_spec.temperature,
        response_schema=tfile_spec.output_schema,
    )
    if isinstance(resp, list):
        resp = resp[0]
    if resp.parsed:
        raw_path = (resp.parsed.get("test_file_path") or "").strip()
        loc.test_is_new = bool(resp.parsed.get("is_new_file", False))
        loc.test_confidence = resp.parsed.get("confidence", "low")
        if raw_path:
            repaired, changed, score = _repair(raw_path, test_files or all_files)
            loc.test_file = repaired
            if changed:
                loc.repaired_filenames.append(f"test_file: {raw_path} -> {repaired}")
                loc.repair_details["test_file"] = {"score": score, "from": raw_path, "to": repaired}
    else:
        logger.debug("localize_test_file: no parsed output (%s)", resp.error)

    # ----- Call 2: focal function -----
    # Pick up to _MAX_FOCAL_FILES focal files (primary first); fall back to
    # test_file's sibling when none of the affected modules resolve.
    focal_paths = _choose_focal_files(issue, all_files, max_files=_MAX_FOCAL_FILES)
    if not focal_paths and loc.test_file and loc.test_file in all_files:
        focal_paths = [loc.test_file]

    if not focal_paths:
        loc.error = "no_focal_file_candidate"
        return loc

    focal_path = focal_paths[0]
    loc.focal_files = focal_paths
    # Build skeleton and known-symbol list over all picked files so the LLM
    # can reach for the right symbol even when it lives in a sibling file.
    skeleton_parts: list[str] = []
    known_symbols: list[str] = []
    for fp in focal_paths:
        src = _fetch_file_source(cid, fp)
        if not src:
            continue
        sk = build_file_skeleton(src) or "(unavailable)"
        skeleton_parts.append(f"# File: {fp}\n{sk}")
        known_symbols.extend(list_symbols(src))
    skeleton = "\n\n".join(skeleton_parts) if skeleton_parts else "(unavailable)"

    focal_spec = load_prompt("localize_focal")
    fctx = dict(
        one_line_summary=issue.one_line_summary or "(unknown)",
        expected_behavior=issue.expected or "(unknown)",
        actual_behavior=issue.actual or "(unknown)",
        traceback=issue.traceback,
        reporter_snippet=issue.reporter_snippet,
        focal_file_path=focal_path,
        file_skeleton=skeleton,
    )
    fresp = complete(
        role=focal_spec.role,
        system=focal_spec.system,
        messages=focal_spec.messages(**fctx),
        temperature=focal_spec.temperature,
        response_schema=focal_spec.output_schema,
    )
    if isinstance(fresp, list):
        fresp = fresp[0]
    if fresp.parsed:
        raw_symbol = (fresp.parsed.get("symbol_name") or "").strip()
        loc.focal_kind = fresp.parsed.get("symbol_kind")
        raw_import = (fresp.parsed.get("import_path") or "").strip() or _path_to_import(focal_path)
        if raw_symbol:
            repaired_sym, changed, _ = _repair(raw_symbol, known_symbols or [raw_symbol])
            loc.focal_symbol = repaired_sym
            if changed:
                loc.repaired_filenames.append(f"focal_symbol: {raw_symbol} -> {repaired_sym}")
                loc.repair_details["focal_symbol"] = {"from": raw_symbol, "to": repaired_sym}
        loc.import_path = raw_import
        loc.focal_file = focal_path

        # v12 A: symbol-aware focal verification. If the LLM-picked symbol
        # is not actually defined in any of the candidate focal files, grep
        # the repo for the symbol's class/function line and promote the
        # matching file to focal_files[0]. This fixes the common case where
        # affected_modules maps to a re-export __init__.py while the symbol
        # lives in a submodule (django-11964), or where the localizer
        # committed to a wrong module entirely (django-12304).
        if loc.focal_symbol and not _symbol_in_known(loc.focal_symbol, known_symbols):
            hits = _grep_symbol_location(cid, loc.focal_symbol, all_files)
            if hits:
                old_focal = loc.focal_file
                new_paths = hits + [p for p in focal_paths if p not in hits]
                loc.focal_files = new_paths[:_MAX_FOCAL_FILES]
                loc.focal_file = hits[0]
                loc.import_path = _path_to_import(hits[0])
                loc.repaired_filenames.append(
                    f"focal_file_symbol_promoted: {old_focal} -> {hits[0]}"
                )
                loc.repair_details["focal_symbol_promoted"] = {
                    "symbol": loc.focal_symbol,
                    "from": old_focal,
                    "to": hits[0],
                    "grep_hits": hits,
                }
    else:
        loc.focal_file = focal_path
        loc.import_path = _path_to_import(focal_path)

    # Coarse confidence: high iff both steps produced parsed output AND had no repair.
    if loc.test_file and loc.focal_symbol and not loc.repair_details:
        loc.confidence = 0.9
    elif loc.test_file and loc.focal_symbol:
        loc.confidence = 0.6
    else:
        loc.confidence = 0.3
    return loc


def _choose_focal_files(
    issue: IssueBundle,
    all_files: list[str],
    *,
    max_files: int = _MAX_FOCAL_FILES,
) -> list[str]:
    """Resolve up to ``max_files`` focal files from issue.affected_modules / traceback.

    Ordered by confidence: direct .py hits first, module-to-path mappings next,
    traceback ``File "..."`` mentions after, then rapidfuzz-repaired affected
    modules as a last resort. Deduplicates while preserving insertion order and
    caps at ``max_files`` to bound prompt size.

    v12 C: filter test paths out of the primary result list. If the filter
    empties everything, fall back to including test paths (with caller aware
    of the weaker signal via `_is_test_path`).
    """
    found: list[str] = []

    def _add(path: str | None) -> bool:
        """Append if new and non-test; return True when we've hit the cap."""
        if path and path not in found and not _is_test_path(path):
            found.append(path)
        return len(found) >= max_files

    candidates: list[str] = []
    # 1) Direct .py matches in affected_modules
    for mod in issue.affected_modules:
        m = mod.strip()
        if not m:
            continue
        if m.endswith(".py") and m in all_files:
            if _add(m):
                return found
        else:
            candidates.append(m)

    # 2) Module-to-path mapping ("a.b.c" → "a/b/c.py", optionally under "src/")
    # v12 B: prefer "a/b/c.py" over "a/b/c/__init__.py" when both exist.
    for mod in candidates:
        path = mod.replace(".", "/") + ".py"
        init_path = mod.replace(".", "/") + "/__init__.py"
        src_path = f"src/{path}"
        src_init = f"src/{init_path}"
        if path in all_files:
            if _add(path):
                return found
        elif src_path in all_files:
            if _add(src_path):
                return found
        elif init_path in all_files:
            if _add(init_path):
                return found
        elif src_init in all_files:
            if _add(src_init):
                return found

    # 3) Traceback: look for File "<path>"
    if issue.traceback:
        import re as _re
        for m in _re.finditer(r'File "([^"]+\.py)"', issue.traceback):
            p = m.group(1)
            if "/testbed/" in p:
                p = p.split("/testbed/", 1)[1]
            if p in all_files:
                if _add(p):
                    return found

    # 4) Last resort: repaired affected modules
    for mod in candidates:
        repaired, changed, _score = _repair(mod, all_files)
        if changed:
            if _add(repaired):
                return found

    # v12 C: if strict filtering left us empty, fall back to including test
    # paths. Caller's downstream logic (symbol verification) can still
    # promote a non-test source file via `_grep_symbol_location`.
    if not found:
        for mod in issue.affected_modules:
            m = mod.strip()
            if m.endswith(".py") and m in all_files and m not in found:
                found.append(m)
                if len(found) >= max_files:
                    break
        for mod in candidates:
            if len(found) >= max_files:
                break
            path = mod.replace(".", "/") + ".py"
            if path in all_files and path not in found:
                found.append(path)

    return found


def _choose_focal_file(issue: IssueBundle, all_files: list[str]) -> str | None:
    """Backward-compatible wrapper returning just the primary focal file."""
    paths = _choose_focal_files(issue, all_files, max_files=1)
    return paths[0] if paths else None

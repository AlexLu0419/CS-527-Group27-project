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


@dataclass
class Localization:
    test_file: str | None = None
    test_is_new: bool = False
    test_confidence: str = "low"
    focal_file: str | None = None
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
    # Pick focal file: the first affected_module that resolves to a repo path; else fall back to test_file's sibling.
    focal_path = _choose_focal_file(issue, all_files)
    if focal_path is None and loc.test_file and loc.test_file in all_files:
        focal_path = loc.test_file

    if focal_path is None:
        loc.error = "no_focal_file_candidate"
        return loc

    source = _fetch_file_source(cid, focal_path)
    skeleton = build_file_skeleton(source) if source else "(unavailable)"
    known_symbols = list_symbols(source)

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


def _choose_focal_file(issue: IssueBundle, all_files: list[str]) -> str | None:
    """Resolve a focal file from the issue's affected_modules / traceback."""
    candidates: list[str] = []
    for mod in issue.affected_modules:
        m = mod.strip()
        if not m:
            continue
        if m.endswith(".py") and m in all_files:
            return m
        candidates.append(m)
    # Try mapping "a.b.c" → "a/b/c.py"
    for mod in candidates:
        path = mod.replace(".", "/") + ".py"
        if path in all_files:
            return path
        if f"src/{path}" in all_files:
            return f"src/{path}"
    # Traceback: look for File "<path>"
    if issue.traceback:
        import re as _re
        for m in _re.finditer(r'File "([^"]+\.py)"', issue.traceback):
            p = m.group(1)
            # make relative to /testbed
            if "/testbed/" in p:
                p = p.split("/testbed/", 1)[1]
            if p in all_files:
                return p
    # Last resort: first affected module repaired
    for mod in candidates:
        repaired, changed, score = _repair(mod, all_files)
        if changed:
            return repaired
    return None

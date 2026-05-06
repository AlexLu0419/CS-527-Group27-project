"""Phase-A cache: one file per instance, strict versioning to prevent stale reuse."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sieve.phases._docker import docker_session
from sieve.prompts import PROMPTS_DIR, load_prompt
from sieve.repro.gate import GatedTest, gate_candidates
from sieve.repro.generate import Candidate, generate_candidates
from sieve.repro.issue import IssueBundle, parse_issue
from sieve.repro.localize import Localization, localize

logger = logging.getLogger("sieve.repro.cache")

PHASE_A_SCHEMA_VERSION = "2b.phaseA.v13"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CACHE_DIR = _REPO_ROOT / "runs" / "phase_a_cache"

# Files whose contents affect the Phase-A pipeline behavior.
_SOURCE_FILES_FOR_FINGERPRINT = [
    "sieve/repro/issue.py",
    "sieve/repro/localize.py",
    "sieve/repro/skeleton.py",
    "sieve/repro/generate.py",
    "sieve/repro/gate.py",
    "sieve/repro/runner.py",
]

_ARTIFACT_PROMPTS = [
    "issue_parser",
    "localize_test_file",
    "localize_focal",
    "mask_raw",
    "mask_traceback_first",
    "mask_snippet_first",
]


def _source_fingerprint() -> str:
    h = hashlib.sha256()
    for rel in _SOURCE_FILES_FOR_FINGERPRINT:
        p = _REPO_ROOT / rel
        h.update(rel.encode())
        if p.exists():
            h.update(p.read_bytes())
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


def _artifact_fingerprint() -> str:
    h = hashlib.sha256()
    for name in _ARTIFACT_PROMPTS:
        spec = load_prompt(name)
        h.update(name.encode())
        h.update(b":" + spec.content_hash().encode() + b"\0")
    return "sha256:" + h.hexdigest()


@dataclass
class PhaseACache:
    instance_id: str
    schema_version: str
    source_fingerprint: str
    artifact_fingerprint: str
    issue: dict
    localization: dict
    candidates: list[dict]
    gated_tests: list[dict]
    zero_signal: bool
    generated_at: str
    cost_usd: float = 0.0
    metadata: dict = field(default_factory=dict)

    def gated(self) -> list[GatedTest]:
        out = []
        for g in self.gated_tests:
            out.append(GatedTest(**g))
        return out

    def surviving(self) -> list[GatedTest]:
        return [g for g in self.gated() if g.bucket != "DROP"]

    def to_dict(self) -> dict:
        return asdict(self)


def cache_path(instance_id: str, base: Path | None = None) -> Path:
    base = base or _DEFAULT_CACHE_DIR
    return base / f"{instance_id}.json"


def _load_raw(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logger.warning("cache: failed to read %s — %s", path, e)
        return None


def _save(path: Path, obj: PhaseACache) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj.to_dict(), ensure_ascii=False, indent=2))
    tmp.replace(path)


def _is_fresh(cached: dict, src_fp: str, art_fp: str) -> bool:
    if cached.get("schema_version") != PHASE_A_SCHEMA_VERSION:
        return False
    if cached.get("source_fingerprint") != src_fp:
        return False
    if cached.get("artifact_fingerprint") != art_fp:
        return False
    return True


def phase_a(
    instance: dict,
    *,
    force: bool = False,
    cache_dir: Path | None = None,
    n_samples_per_mask: int = 2,
    temperature: float = 1.0,
    gate_timeout: int = 60,
) -> PhaseACache:
    """Run Phase A (generate + gate) for an instance, with on-disk caching."""
    iid = instance["instance_id"]
    base = cache_dir or _DEFAULT_CACHE_DIR
    path = cache_path(iid, base=base)

    src_fp = _source_fingerprint()
    art_fp = _artifact_fingerprint()

    if not force:
        cached = _load_raw(path)
        if cached is not None:
            if _is_fresh(cached, src_fp, art_fp):
                return PhaseACache(**cached)
            logger.info(
                "phase_a: stale cache for %s (schema/src/art mismatch) — regenerating",
                iid,
            )

    # 1) Parse issue
    issue = parse_issue(instance)

    # 2) Open a single container for localization + gating
    with docker_session(iid) as cid:
        if cid is None:
            empty = PhaseACache(
                instance_id=iid,
                schema_version=PHASE_A_SCHEMA_VERSION,
                source_fingerprint=src_fp,
                artifact_fingerprint=art_fp,
                issue=issue.to_dict(),
                localization={},
                candidates=[],
                gated_tests=[],
                zero_signal=True,
                generated_at=_iso_now(),
                metadata={"error": "container_startup_failed"},
            )
            _save(path, empty)
            return empty

        loc = localize(issue, cid=cid)

        # 3) Generate candidates
        candidates = generate_candidates(
            issue,
            loc,
            n_samples_per_mask=n_samples_per_mask,
            temperature=temperature,
            repo=instance.get("repo", ""),
        )

        # 4) Gate candidates on the unpatched repo.
        gated = gate_candidates(
            instance,
            candidates,
            issue=issue,
            timeout=gate_timeout,
            cid=cid,
        )

    surviving = [g for g in gated if g.bucket != "DROP"]
    cache = PhaseACache(
        instance_id=iid,
        schema_version=PHASE_A_SCHEMA_VERSION,
        source_fingerprint=src_fp,
        artifact_fingerprint=art_fp,
        issue=issue.to_dict(),
        localization=loc.to_dict(),
        candidates=[c.to_dict() for c in candidates],
        gated_tests=[g.to_dict() for g in gated],
        zero_signal=len(surviving) == 0,
        generated_at=_iso_now(),
    )
    _save(path, cache)
    return cache


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Re-exports for convenience
__all__ = [
    "PHASE_A_SCHEMA_VERSION",
    "PhaseACache",
    "cache_path",
    "phase_a",
    "Candidate",
    "GatedTest",
    "IssueBundle",
    "Localization",
]

"""Artifact version tracking + per-layer snapshot hashing.

Used by the cascade driver to know which cached layer results are still valid
after a prompt edit (cycle 1).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sieve.prompts import PROMPTS_DIR, load_prompt

ARTIFACT_VERSIONS_JSON = PROMPTS_DIR / "ARTIFACT_VERSIONS.json"

LAYER_DEPS: dict[str, list[str]] = {
    "static":       [],
    "regression":   [],
    "phase_a":      [
        "issue_parser",
        "localize_test_file",
        "localize_focal",
        "mask_raw",
        "mask_traceback_first",
        "mask_snippet_first",
    ],
    "phase_b":      [
        "mask_raw",
        "mask_traceback_first",
        "mask_snippet_first",
    ],
    "judge":        ["judge_patch"],
    "feedback":     ["feedback_synth"],
}


def load_artifact_versions() -> dict[str, dict]:
    if not ARTIFACT_VERSIONS_JSON.exists():
        return {}
    return json.loads(ARTIFACT_VERSIONS_JSON.read_text())


def current_versions() -> dict[str, dict]:
    """Return the live content hashes + versions for every prompt file on disk."""
    out: dict[str, dict] = {}
    for p in sorted(PROMPTS_DIR.glob("*.yaml")):
        spec = load_prompt(p.stem)
        out[p.stem] = {"version": spec.version, "content_hash": spec.content_hash()}
    return out


def snapshot_hash(layer: str) -> str:
    """Stable hash identifying the artifact state for one cascade layer."""
    deps = LAYER_DEPS.get(layer, [])
    if not deps:
        return "layer-free"
    versions = current_versions()
    h = hashlib.sha256()
    h.update(layer.encode())
    for name in sorted(deps):
        entry = versions.get(name, {"version": 0, "content_hash": "sha256:missing"})
        h.update(name.encode())
        h.update(b":" + str(entry["version"]).encode())
        h.update(b":" + entry["content_hash"].encode())
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


def all_layer_snapshots() -> dict[str, str]:
    return {layer: snapshot_hash(layer) for layer in LAYER_DEPS}


def verify_artifact_versions() -> list[tuple[str, str, str]]:
    """Return list of (name, recorded_hash, current_hash) for entries that drifted.
    Used as a pre-flight check for cascade runs."""
    recorded = load_artifact_versions()
    current = current_versions()
    diffs: list[tuple[str, str, str]] = []
    for name, entry in current.items():
        rec = recorded.get(name, {})
        if rec.get("content_hash") != entry["content_hash"]:
            diffs.append((name, rec.get("content_hash", "<missing>"), entry["content_hash"]))
    return diffs

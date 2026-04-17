"""Input hygiene for the judge — strip agent narrative + patch provenance."""
from __future__ import annotations

import re

_AGENT_STANZA_RE = re.compile(
    r"^(Thought|Action|Observation|Reasoning|Plan|Reflection)\s*:\s*.*?(?=\n(?:Thought|Action|Observation|Reasoning|Plan|Reflection|$))",
    re.DOTALL | re.MULTILINE,
)
_COT_MARKERS_RE = re.compile(r"<\|?(?:thinking|cot|scratchpad)\|?>.*?<\|?/(?:thinking|cot|scratchpad)\|?>", re.DOTALL | re.IGNORECASE)
_PATCH_PROVENANCE_RE = re.compile(
    r"^(From|Date|Subject|Signed-off-by|Co-authored-by|Author|Reviewed-by)\s*:.*$",
    re.MULTILINE | re.IGNORECASE,
)
_FIX_COMMENT_RE = re.compile(
    r"^\+?\s*(?:#|//)\s*(?:Fix|Fixes|Fixed|Resolves?|Closes?)\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_ISSUE_REF_RE = re.compile(r"\bissue\s*#\s*\d+\b", re.IGNORECASE)


def strip_agent_narrative(text: str) -> str:
    if not text:
        return text
    out = _COT_MARKERS_RE.sub("", text)
    out = _AGENT_STANZA_RE.sub("", out)
    return out.strip()


def strip_patch_metadata(diff: str) -> str:
    if not diff:
        return diff
    out = _PATCH_PROVENANCE_RE.sub("", diff)
    out = _FIX_COMMENT_RE.sub("", out)
    out = _ISSUE_REF_RE.sub("(issue-ref redacted)", out)
    # collapse 3+ consecutive blank lines
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() + ("\n" if diff.endswith("\n") else "")

"""Generate reproduction test candidates across 3 masks × N samples.

Its tests routed to A or C at the gate (never B) and the judge rated the resulting
reproduction tests as *less* legitimate (`failing_tests_legitimate` dropped 0.136 → 0.062).
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass

from sieve.llm import complete
from sieve.llm.roles import resolve_model
from sieve.prompts import load_prompt
from sieve.repro.issue import IssueBundle
from sieve.repro.localize import Localization

logger = logging.getLogger("sieve.repro.generate")


@dataclass
class Candidate:
    cand_id: str
    mask: str              # "raw" | "traceback" | "snippet"
    sample_idx: int
    source: str            # the extracted Python script
    model: str
    raw_response: str = ""
    parse_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


def _extract_code(text: str) -> str:
    m = _CODE_BLOCK_RE.search(text or "")
    if m:
        return m.group(1).strip() + "\n"
    # Fallback: assume the entire response is code
    return (text or "").strip() + "\n"


def _ensure_exit_convention(code: str) -> str:
    """Safety net: if the generated script forgets to import sys, prepend it."""
    if re.search(r"\bimport sys\b", code):
        return code
    return "import sys\n" + code


def _issue_title_body(issue: IssueBundle) -> tuple[str, str]:
    raw = issue.raw_text or ""
    first, _, rest = raw.partition("\n")
    first_s = first.strip()
    if first_s and len(first_s) < 200 and not first_s.endswith("."):
        return first_s, rest.lstrip("\n")
    return issue.one_line_summary, issue.body or raw


_SPHINX_HINT = """\
This repository is **Sphinx**.
- The script is executed as `python <file>.py` (no pytest), so you CANNOT use `@pytest.mark.sphinx`, `SphinxTestApp` fixtures, or `tests/roots/test-*` testroots directly.
- To drive an actual Sphinx build, synthesize a minimal site in a tempdir and instantiate the Sphinx application programmatically:
    ```python
    import tempfile, os
    from pathlib import Path
    from sphinx.application import Sphinx

    src = Path(tempfile.mkdtemp())
    (src / "conf.py").write_text("project='t'\\nextensions=['sphinx.ext.autodoc']\\nmaster_doc='index'\\n")
    (src / "index.rst").write_text(".. autoclass:: mymod.MyClass\\n")
    out = src / "_build"
    app = Sphinx(str(src), str(src), str(out), str(out / ".doctrees"), buildername="html")
    app.build()
    # then assert on (out / "index.html").read_text() or on app.env.*
    ```
- For pure RST/docutils parsing bugs (no full build needed), import `sphinx.parsers`, `sphinx.util.docutils`, or `docutils.utils.new_document` directly.
- For autodoc-specific bugs, create a small Python module on `sys.path`, then run a minimal build against a conf.py that enables `sphinx.ext.autodoc` and an index.rst containing `.. autoclass:: / .. autofunction::`.
"""

_DJANGO_HINT = """\
This repository is **Django**.
- The script is executed as `python <file>.py`. BEFORE importing any django models/forms/widgets, configure settings + call setup:
    ```python
    import django
    from django.conf import settings
    if not settings.configured:
        settings.configure(
            DEBUG=True,
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                # add the specific app(s) touched by the bug here
            ],
            USE_TZ=True,
        )
    django.setup()
    ```
- For model/ORM bugs: after `django.setup()`, call `from django.core.management import call_command; call_command('migrate', run_syncdb=True, verbosity=0)` before using the ORM.
- For forms/widgets bugs: settings configuration alone is usually enough — no DB needed — just import the widget/form and exercise it.
- Do NOT use `django.test.TestCase`, `SimpleTestCase`, or `manage.py test` — they require a test runner. Use plain `assert` and the print/exit convention.
- For admin/utils bugs, you may need to install the `admin` / `sessions` apps; read the bug's traceback to decide.
"""


def _test_framework_hint(repo: str) -> str:
    repo_lc = (repo or "").lower()
    if "sphinx" in repo_lc:
        return _SPHINX_HINT
    if "django" in repo_lc:
        return _DJANGO_HINT
    return ""


def _call_mask(
    spec_name: str,
    ctx: dict,
    *,
    n: int,
    temperature: float,
) -> list:
    spec = load_prompt(spec_name)
    model = resolve_model(spec.role)
    resp = complete(
        role=spec.role,
        system=spec.system,
        messages=spec.messages(**ctx),
        temperature=temperature if temperature is not None else spec.temperature,
        n=n,
    )
    responses = resp if isinstance(resp, list) else [resp]
    return [(model, r) for r in responses]


def generate_candidates(
    issue: IssueBundle,
    localization: Localization,
    *,
    n_samples_per_mask: int = 2,
    temperature: float = 1.0,
    repo: str = "",
) -> list[Candidate]:
    """Produce up to 3 * n_samples_per_mask candidates.

    Mask 2 (traceback) is skipped if ``issue.traceback`` is missing.
    Mask 3 (snippet) falls back to Mask 1 when ``issue.reporter_snippet`` is missing,
    tagged with sample_idx offset to distinguish from the primary Mask-1 samples.
    """
    title, body = _issue_title_body(issue)
    affected_modules_joined = "\n".join(f"- {m}" for m in (issue.affected_modules or []))
    common = dict(
        issue_title=title,
        issue_body=body,
        one_line_summary=issue.one_line_summary or title,
        expected_behavior=issue.expected or "(unknown)",
        actual_behavior=issue.actual or "(unknown)",
        affected_modules_joined=affected_modules_joined,
        traceback=issue.traceback or "",
        reporter_snippet=issue.reporter_snippet or "",
        exception_type=issue.exception_type or "",
        symbol_name=localization.focal_symbol or "",
        import_path=localization.import_path or "",
        test_framework_hint=_test_framework_hint(repo),
    )

    candidates: list[Candidate] = []

    # Mask 1 — raw, always
    for idx, (model, r) in enumerate(_call_mask("mask_raw", common, n=n_samples_per_mask, temperature=temperature)):
        code = _ensure_exit_convention(_extract_code(r.text))
        candidates.append(Candidate(
            cand_id=f"raw_{idx}",
            mask="raw",
            sample_idx=idx,
            source=code,
            model=model,
            raw_response=r.text,
            parse_error=r.error,
        ))

    # Mask 2 — traceback-first, only if traceback present
    if issue.traceback:
        for idx, (model, r) in enumerate(_call_mask("mask_traceback_first", common, n=n_samples_per_mask, temperature=temperature)):
            code = _ensure_exit_convention(_extract_code(r.text))
            candidates.append(Candidate(
                cand_id=f"traceback_{idx}",
                mask="traceback",
                sample_idx=idx,
                source=code,
                model=model,
                raw_response=r.text,
                parse_error=r.error,
            ))
    else:
        logger.debug("generate_candidates: skipping mask_traceback_first (no traceback)")

    # Mask 3 — snippet-first, else fall back to Mask 1 with offset
    if issue.reporter_snippet:
        for idx, (model, r) in enumerate(_call_mask("mask_snippet_first", common, n=n_samples_per_mask, temperature=temperature)):
            code = _ensure_exit_convention(_extract_code(r.text))
            candidates.append(Candidate(
                cand_id=f"snippet_{idx}",
                mask="snippet",
                sample_idx=idx,
                source=code,
                model=model,
                raw_response=r.text,
                parse_error=r.error,
            ))
    else:
        logger.debug("generate_candidates: no reporter_snippet, falling back to mask_raw")
        for idx, (model, r) in enumerate(_call_mask("mask_raw", common, n=n_samples_per_mask, temperature=temperature)):
            code = _ensure_exit_convention(_extract_code(r.text))
            # offset sample_idx to disambiguate in logs
            candidates.append(Candidate(
                cand_id=f"raw_{idx + n_samples_per_mask}",
                mask="raw",
                sample_idx=idx + n_samples_per_mask,
                source=code,
                model=model,
                raw_response=r.text,
                parse_error=r.error,
            ))

    return candidates

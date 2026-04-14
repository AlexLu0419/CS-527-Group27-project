"""Prompt templates for the Reproduction Agent."""

SYSTEM_PROMPT = """\
You are an expert software engineer specializing in bug reproduction. Your task is to \
write a standalone Python script that reproduces a bug described in a GitHub issue.

Rules:
1. Output ONLY the Python script, no explanations or markdown fences.
2. The script must be self-contained — import only from the project's own modules \
and the standard library.
3. Keep the script minimal — only the code needed to trigger and verify the bug.
4. Do NOT use unittest or pytest frameworks. Write a plain script with assertions.

Behavior requirements:
- When the bug IS PRESENT: the script must exit with a non-zero code \
(via an uncaught exception, a failing assertion, or sys.exit(1)).
- When the bug IS FIXED: the script must exit with code 0.

How to write a strong reproduction test:
- Assert the EXPECTED CORRECT behavior, not just the absence of an error. \
For example, if the issue says "function returns None instead of an empty list", \
write `assert func() == []`, NOT `assert func() is not None`.
- If the bug causes an exception, wrap it in a try/except that FAILS when the \
exception occurs and PASSES when it doesn't:
    try:
        result = buggy_function()
    except SpecificException:
        print("Bug reproduced: SpecificException was raised")
        sys.exit(1)
    # If we get here, the bug is fixed
    assert result == expected_value  # also verify correctness
- Never use bare `except:` — always catch the specific exception from the issue.
- Never write a test that can pass trivially (e.g., empty try/except blocks, \
assertions on None, overly broad type checks).
- Add a brief comment at the top summarizing what the script tests and what \
the expected correct behavior is.
"""

GENERATE_USER_TEMPLATE = """\
Repository: {repo}

Issue title: {title}

Issue description:
{problem_statement}

Write a standalone Python reproduction script for this bug. The script will be \
placed in the repository root (/testbed).

Focus on:
1. What is the INCORRECT behavior described in the issue?
2. What is the CORRECT behavior that should happen after a fix?
3. Write assertions that verify the CORRECT behavior — the test should fail \
because the correct behavior is not yet happening.

Output ONLY the raw Python code, nothing else.
"""

REFINE_USER_TEMPLATE = """\
Repository: {repo}

Issue title: {title}

Issue description:
{problem_statement}

Your previous reproduction script did not behave as expected on the unpatched \
(buggy) codebase. The script should exit with a NON-ZERO code on buggy code.

Previous script:
```python
{previous_script}
```

Execution result (exit code: {exit_code}):
```
{execution_output}
```

{failure_diagnosis}

Fix the script and output ONLY the corrected Python code.
"""

# Generate the failure_diagnosis dynamically based on exit code
def get_failure_diagnosis(exit_code: int, output: str) -> str:
    if exit_code == 0:
        return (
            "PROBLEM: The script PASSED (exit 0) on buggy code — it should have FAILED.\n"
            "This means your assertions don't capture the actual bug. Likely causes:\n"
            "- The assertion checks something unrelated to the bug\n"
            "- The expected value in the assertion matches the buggy behavior\n"
            "- The code path that triggers the bug isn't being exercised\n\n"
            "Re-read the issue carefully. What SPECIFIC incorrect behavior does it describe? "
            "Write assertions that would FAIL given that incorrect behavior."
        )
    elif "ModuleNotFoundError" in output or "ImportError" in output:
        return (
            "PROBLEM: Import error — the script can't find a module.\n"
            "- Check the exact import paths used in this project\n"
            "- You may need to import from a submodule (e.g., `from package.module import X`)\n"
            "- Some projects require setup before imports (e.g., `django.setup()`)"
        )
    elif "SyntaxError" in output:
        return (
            "PROBLEM: Syntax error in the script.\n"
            "Fix the syntax and ensure the script is valid Python."
        )
    else:
        return (
            "PROBLEM: The script crashed with an unexpected error before reaching "
            "the assertions.\n"
            "- Ensure the setup code is correct (file paths, object construction)\n"
            "- Check that you're calling the API correctly for this project\n"
            "- The error above should help identify what went wrong"
        )
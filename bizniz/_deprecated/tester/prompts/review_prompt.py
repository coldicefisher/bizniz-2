REVIEW_PROMPT_TEMPLATE = """
Review and strengthen an existing pytest test suite.

PROBLEM STATEMENT (what the code is supposed to do):
──────────────────────────────────────────────────────────────
{problem_statement}

MODULE NAME:
──────────────────────────────────────────────────────────────
{module_name}

CODE UNDER TEST:
──────────────────────────────────────────────────────────────
{code}

EXISTING TESTS:
──────────────────────────────────────────────────────────────
{existing_tests}

TASK:
Analyse the existing tests and produce an improved version that:
1. Imports symbols from the module named above (e.g. `from {module_name} import ...`).
2. Always includes `import pytest` at the top.
3. Keeps all currently passing tests (do not remove working tests).
4. Adds missing edge cases: boundary values, empty inputs, large inputs,
   invalid types, off-by-one errors, etc.
5. Strengthens weak assertions (e.g. replace bare `assert result` with
   `assert result == expected_value, f"got {{result}}"`).
6. Adds parametrize decorators where multiple similar cases exist.
7. Fixes any tests that would fail even with a correct implementation.
8. Organises tests into logical sections with comments.

Return the COMPLETE improved test file (not a diff), as valid JSON with key "tests".
"""

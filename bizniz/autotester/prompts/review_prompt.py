REVIEW_PROMPT_TEMPLATE = """
Review and strengthen an existing pytest test suite.

PROBLEM STATEMENT (what the code is supposed to do):
──────────────────────────────────────────────────────────────
{problem_statement}

CODE UNDER TEST:
──────────────────────────────────────────────────────────────
{code}

EXISTING TESTS:
──────────────────────────────────────────────────────────────
{existing_tests}

TASK:
Analyse the existing tests and produce an improved version that:
1. Keeps all currently passing tests (do not remove working tests).
2. Adds missing edge cases: boundary values, empty inputs, large inputs,
   invalid types, off-by-one errors, etc.
3. Strengthens weak assertions (e.g. replace bare `assert result` with
   `assert result == expected_value, f"got {{result}}"`).
4. Adds parametrize decorators where multiple similar cases exist.
5. Fixes any tests that would fail even with a correct implementation.
6. Organises tests into logical sections with comments.

Return the COMPLETE improved test file (not a diff), as valid JSON with key "tests".
"""

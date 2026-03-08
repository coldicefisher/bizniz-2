FROM_CODE_PROMPT_TEMPLATE = """
Write a complete pytest test suite for the following Python code.

PROBLEM STATEMENT (what the code is supposed to do):
──────────────────────────────────────────────────────────────
{problem_statement}

CODE UNDER TEST:
──────────────────────────────────────────────────────────────
{code}

TASK:
Write a complete pytest test file. The tests should:
1. Import the relevant symbols from the module (assume the module is on sys.path).
2. Verify correct behaviour for normal inputs derived from the problem statement.
3. Cover edge cases: empty inputs, boundary values, negative numbers, zero, etc.
4. Use pytest.mark.parametrize for any group of similar input/output pairs.
5. Test any error handling implied by the problem statement.
6. Include at least one assertion message in each test for debuggability.

Return ONLY valid JSON with key "tests" containing the full test source.
"""

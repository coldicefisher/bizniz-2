FROM_CODE_PROMPT_TEMPLATE = """
Write a complete pytest test suite for the following Python code.

PROBLEM STATEMENT (what the code is supposed to do):
──────────────────────────────────────────────────────────────
{problem_statement}

MODULE NAME:
──────────────────────────────────────────────────────────────
{module_name}

CODE UNDER TEST:
──────────────────────────────────────────────────────────────
{code}

TASK:
Write a complete pytest test file. The tests should:
1. Import the relevant symbols from the module named above.
   For example: `from {module_name} import <function_or_class_name>`
2. Always include `import pytest` at the top.
3. Verify correct behaviour for normal inputs derived from the problem statement.
4. Cover edge cases: empty inputs, boundary values, negative numbers, zero, etc.
5. Use pytest.mark.parametrize for any group of similar input/output pairs.
6. Test any error handling implied by the problem statement.
7. Include at least one assertion message in each test for debuggability.

Return ONLY valid JSON with key "tests" containing the full test source.
"""

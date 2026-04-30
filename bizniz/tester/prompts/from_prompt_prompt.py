FROM_PROMPT_PROMPT_TEMPLATE = """
Write a pytest test suite based on a problem statement alone.
No implementation exists yet — your tests will define the contract that a correct
implementation must satisfy. Think of this as test-driven development.

PROBLEM STATEMENT:
──────────────────────────────────────────────────────────────
{problem_statement}

MODULE NAME:
──────────────────────────────────────────────────────────────
{module_name}

TASK:
Infer the expected public interface from the problem statement and write tests that
a correct implementation must pass.

Guidelines:
- IMPORTANT: Import the function/class under test from the module specified above.
  For example: `from {module_name} import <function_or_class_name>`
- Add a comment block at the top of the test file documenting the assumed interface.
- Tests must be written so they will pass without modification once the implementation exists.
- Cover the happy path, boundary conditions, and any error cases implied by the description.
- Use pytest.mark.parametrize for tables of inputs and expected outputs.
- Do not write the implementation — only the tests.
- Always include `import pytest` at the top of the test file.

Return ONLY valid JSON with key "tests" containing the full test source.
"""

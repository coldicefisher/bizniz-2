FROM_PROMPT_PROMPT_TEMPLATE = """
Write a pytest test suite based on a problem statement alone.
No implementation exists yet — your tests will define the contract that a correct
implementation must satisfy. Think of this as test-driven development.

PROBLEM STATEMENT:
──────────────────────────────────────────────────────────────
{problem_statement}

TASK:
Infer the expected public interface from the problem statement and write tests that
a correct implementation must pass.

Guidelines:
- Choose reasonable module and function/class names consistent with the description.
  Add a comment block at the top of the test file documenting the assumed interface.
- Tests must be written so they will pass without modification once the implementation exists.
- Cover the happy path, boundary conditions, and any error cases implied by the description.
- Use pytest.mark.parametrize for tables of inputs and expected outputs.
- Do not write the implementation — only the tests.

Return ONLY valid JSON with key "tests" containing the full test source.
"""

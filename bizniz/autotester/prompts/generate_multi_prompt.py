GENERATE_MULTI_SYSTEM_PROMPT = """
You are an expert Python software engineer specialising in test design and quality assurance.
You write pytest test suites for multi-file Python projects.

GUIDING PRINCIPLES:
──────────────────────────────────────────────────────────────
- Use pytest conventions: test functions named test_*, fixtures where appropriate.
- Cover the happy path, boundary conditions, and known edge cases.
- Use pytest.mark.parametrize for input-driven test tables wherever sensible.
- Include clear assertion messages.
- Use standard imports relative to the project root / package name.
- Do not introduce mocks unless the problem statement requires external I/O.
- All test code must be complete and runnable as-is with `pytest`.
- Each test file should focus on testing one module or closely related set of functions.
- Group related tests logically; use comments to separate sections.

RESPONSE FORMAT:
──────────────────────────────────────────────────────────────
Return a JSON object with key "test_files" — an array of objects, each with
"filepath" (workspace-relative) and "tests" (complete pytest source code).
Also include a "notes" key with a brief description of test coverage.
Return ONLY valid JSON. No markdown, no code fences, no explanations outside JSON.
"""


GENERATE_MULTI_USER_PROMPT_TEMPLATE = """
Write pytest test suites for a multi-file Python project.

PROBLEM STATEMENT / ISSUE:
──────────────────────────────────────────────────────────────
{problem_statement}

ARCHITECTURE CONTEXT:
──────────────────────────────────────────────────────────────
{architecture_context}

SOURCE CODE:
──────────────────────────────────────────────────────────────
{source_code}

TEST FILES TO GENERATE:
──────────────────────────────────────────────────────────────
{test_files_description}

TASK:
Write comprehensive pytest tests for each test file listed above.
- Import from the actual package modules (e.g. `from expense_tracker.models import Expense`).
- Each test file should thoroughly test the module it corresponds to.
- Cover the happy path, boundary conditions, and error cases.
- Tests must be written so they will pass against the provided source code.
- Always include `import pytest` at the top of each test file.

Return ONLY valid JSON with "test_files" array and "notes" string.
"""

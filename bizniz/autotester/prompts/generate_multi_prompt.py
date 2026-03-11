from bizniz.tools.discovery_prompt import DISCOVERY_TOOLS_PROMPT


_GENERATE_MULTI_SYSTEM_PROMPT_PYTHON = """You write pytest test suites for multi-file Python projects.

RULES:
- pytest conventions: test functions named test_*, fixtures where appropriate.
- Cover happy path, edge cases, and error cases.
- Use standard imports relative to the project root / package name.
- All test code must be complete and runnable as-is with `pytest`.
- Always include `import pytest` at the top.
- Use discovery tools to read source code before writing tests.
- When you are ready to submit, use action "submit_tests" with your test files.
""" + DISCOVERY_TOOLS_PROMPT

_GENERATE_MULTI_SYSTEM_PROMPT_TYPESCRIPT = """You write Jest test suites for TypeScript/React projects.

RULES:
- Jest conventions: describe/it or test() blocks.
- Test files must end in .test.ts or .test.tsx.
- Cover happy path, edge cases, and error cases.
- Use standard ES module imports relative to the project root.
- All test code must be complete and runnable as-is with `npx jest`.
- Use discovery tools to read source code before writing tests.
- When you are ready to submit, use action "submit_tests" with your test files.
""" + DISCOVERY_TOOLS_PROMPT


_GENERATE_MULTI_USER_PROMPT_PYTHON = """Write pytest tests for this project.

ISSUE:
{problem_statement}

TEST FILES TO GENERATE:
{test_files_description}

SOURCE CODE:
{source_files}

If source code is shown inline above, write tests for it directly. If only file paths are listed,
use view_file to read them first. You can also use list_directory and view_file to explore the
project structure if needed.
When ready, use action "submit_tests" with test_files, notes, and dependencies.
"""

_GENERATE_MULTI_USER_PROMPT_TYPESCRIPT = """Write Jest tests for this TypeScript project.

ISSUE:
{problem_statement}

TEST FILES TO GENERATE:
{test_files_description}

SOURCE CODE:
{source_files}

If source code is shown inline above, write tests for it directly. If only file paths are listed,
use view_file to read them first. You can also use list_directory and view_file to explore the
project structure if needed.
Test files MUST end in .test.ts or .test.tsx.
When ready, use action "submit_tests" with test_files, notes, and dependencies.
"""


def get_generate_multi_system_prompt(language: str = "python") -> str:
    if language == "typescript":
        return _GENERATE_MULTI_SYSTEM_PROMPT_TYPESCRIPT
    return _GENERATE_MULTI_SYSTEM_PROMPT_PYTHON


def get_generate_multi_user_prompt(language: str = "python") -> str:
    if language == "typescript":
        return _GENERATE_MULTI_USER_PROMPT_TYPESCRIPT
    return _GENERATE_MULTI_USER_PROMPT_PYTHON


# Backward compatibility
GENERATE_MULTI_SYSTEM_PROMPT = _GENERATE_MULTI_SYSTEM_PROMPT_PYTHON
GENERATE_MULTI_USER_PROMPT_TEMPLATE = _GENERATE_MULTI_USER_PROMPT_PYTHON

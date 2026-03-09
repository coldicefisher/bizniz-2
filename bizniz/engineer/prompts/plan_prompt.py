_ARCHITECTURE_PLAN_PROMPT_PYTHON = """
Plan the software architecture for the following project.

PROBLEM STATEMENT:
──────────────────────────────────────────────────────────────
{problem_statement}

REQUIREMENTS:
──────────────────────────────────────────────────────────────
{requirements_text}

USE CASES:
──────────────────────────────────────────────────────────────
{use_cases_text}

Design the project as a proper Python package. Define:

1. PACKAGE NAME: A snake_case package name for the project.

2. NAMESPACES: Directory structure within the package. Each namespace is a
   directory with an __init__.py. Think about logical grouping:
   - models/ for domain types and data classes
   - services/ or core/ for business logic
   - cli/ or api/ for entry points (if applicable)
   - utils/ for shared utilities

3. DOMAIN MODELS: Shared types and data classes used across multiple modules.
   These are the project's vocabulary — the nouns of the system. Define:
   - Class name, filepath, and namespace
   - Fields with type hints
   - Method signatures (just signatures, not implementations)
   - A brief docstring

4. MODULES: Implementation classes/functions. Define:
   - Filepath, class name (if applicable), and namespace
   - Method signatures with descriptions
   - A brief docstring

5. DEPENDENCIES: Import edges between modules. Which module imports what from where.

RULES:
- All file paths are relative to the workspace root
- All paths must be inside the package namespace (e.g. expense_tracker/models/expense.py)
- Domain models come FIRST — they have no dependencies on implementation modules
- Implementation modules depend on domain models, not the other way around
- Keep the design minimal — only what's needed for the requirements
- Use standard Python conventions (snake_case files, PascalCase classes)

Return ONLY valid JSON matching the schema. No markdown, no code fences.
"""

_ARCHITECTURE_PLAN_PROMPT_TYPESCRIPT = """
Plan the software architecture for the following project.

PROBLEM STATEMENT:
──────────────────────────────────────────────────────────────
{problem_statement}

REQUIREMENTS:
──────────────────────────────────────────────────────────────
{requirements_text}

USE CASES:
──────────────────────────────────────────────────────────────
{use_cases_text}

CRITICAL: This is a TypeScript project. Do NOT use Python conventions.
All files must use .ts or .tsx extensions. No .py files, no __init__.py, no pyproject.toml.

Design the project as a TypeScript project. Define:

1. PACKAGE NAME: A kebab-case or camelCase project name.

2. NAMESPACES: Directory structure within the src/ directory. Think about logical grouping:
   - src/types/ or src/models/ for TypeScript interfaces and types
   - src/services/ or src/core/ for business logic
   - src/components/ for React components (if applicable)
   - src/utils/ for shared utilities
   - src/__tests__/ for test files

3. DOMAIN MODELS: Shared TypeScript interfaces and types used across multiple modules.
   These are the project's vocabulary — the nouns of the system. Define:
   - Interface/type name, filepath (must end in .ts), and namespace
   - Fields with TypeScript types
   - Method signatures (just signatures, not implementations)
   - A brief description

4. MODULES: Implementation classes/functions. Define:
   - Filepath (must end in .ts or .tsx), class/function name, and namespace
   - Method signatures with descriptions
   - A brief description

5. DEPENDENCIES: Import edges between modules. Which module imports what from where.

RULES:
- All file paths must use .ts or .tsx extensions
- All paths should be inside src/ (e.g. src/models/counter.ts, src/components/App.tsx)
- Domain models (interfaces/types) come FIRST — they have no dependencies on implementation modules
- Implementation modules depend on domain models, not the other way around
- Keep the design minimal — only what's needed for the requirements
- Use standard TypeScript conventions (camelCase functions, PascalCase classes/interfaces)
- Do NOT create any Python files (.py), __init__.py, or pyproject.toml

Return ONLY valid JSON matching the schema. No markdown, no code fences.
"""


def get_architecture_plan_prompt(language: str = "python") -> str:
    if language == "typescript":
        return _ARCHITECTURE_PLAN_PROMPT_TYPESCRIPT
    return _ARCHITECTURE_PLAN_PROMPT_PYTHON


# Backward compatibility
ARCHITECTURE_PLAN_PROMPT_TEMPLATE = _ARCHITECTURE_PLAN_PROMPT_PYTHON

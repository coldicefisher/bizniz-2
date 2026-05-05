_ARCHITECTURE_PLAN_PROMPT_PYTHON = """
Plan the software architecture for the following project.

{skeleton_contract}

{auth_context}

PROBLEM STATEMENT:
──────────────────────────────────────────────────────────────
{problem_statement}

REQUIREMENTS:
──────────────────────────────────────────────────────────────
{requirements_text}

USE CASES:
──────────────────────────────────────────────────────────────
{use_cases_text}

Design the project's architecture. Define:

1. PACKAGE NAME: If a Skeleton directory contract is shown above,
   set this to the skeleton's existing root package (e.g. "app" for
   the FastAPI skeleton). DO NOT invent a new package name when a
   skeleton is present — its layout already exists on disk.
   Otherwise, choose a snake_case package name for the project.

2. NAMESPACES: Directory structure inside the package. If a
   skeleton contract is present, namespaces MUST be the skeleton's
   declared extension points (e.g. for FastAPI: api/routes/,
   models/, schemas/, services/). Add new feature subdirectories
   only where the contract permits. Without a skeleton, choose
   logical groupings (models/, services/, api/, utils/).

3. DOMAIN MODELS: Shared types and data classes used across
   multiple modules. These are the project's vocabulary — the
   nouns of the system. Define:
   - Class name, filepath, and namespace
   - Fields with type hints
   - Method signatures (just signatures, not implementations)
   - A brief docstring

4. MODULES: Implementation classes/functions. Define:
   - Filepath, class name (if applicable), and namespace
   - Method signatures with descriptions
   - A brief docstring

5. DEPENDENCIES: Import edges between modules. Which module
   imports what from where.

RULES:
- All file paths are relative to the workspace root.
- When a skeleton contract is present, file paths MUST be inside
  the skeleton's extension points (e.g. app/api/routes/services.py,
  app/models/service.py, app/schemas/services.py for the FastAPI
  skeleton). NEVER place files in a parallel package outside the
  skeleton's root (e.g. pet_groomer/, my_app/). Files outside the
  skeleton's root are dead code — the running container's
  entrypoint can't reach them.
- Without a skeleton, all paths must be inside the package
  namespace (e.g. expense_tracker/models/expense.py).
- Domain models come FIRST — they have no dependencies on
  implementation modules.
- Implementation modules depend on domain models, not the other
  way around.
- Keep the design minimal — only what's needed for the requirements.
- Use standard Python conventions (snake_case files, PascalCase
  classes).

Return ONLY valid JSON matching the schema. No markdown, no code
fences.
"""

_ARCHITECTURE_PLAN_PROMPT_TYPESCRIPT = """
Plan the software architecture for the following project.

{skeleton_contract}

{auth_context}

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

When a Skeleton directory contract is shown above, place files in
the skeleton's declared extension points (e.g. for the React
skeleton: src/pages/<feature>.tsx, src/api/<feature>.ts,
src/routes/<feature>.tsx). NEVER create a parallel src/components/
or src/services/ tree that bypasses the skeleton's existing files.

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

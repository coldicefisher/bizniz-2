ARCHITECTURE_PLAN_PROMPT_TEMPLATE = """
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

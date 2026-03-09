ANALYZE_PROMPT_TEMPLATE = """
Analyze the following problem statement and produce a complete engineering breakdown.

PROBLEM STATEMENT:
──────────────────────────────────────────────────────────────
{problem_statement}

{architecture_context}

Produce a JSON response with:
- "business_requirements": list of strings (business goals served)
- "use_cases": list of objects with "title" and "description"
- "functional_requirements": list of strings (concrete capabilities)
- "nonfunctional_requirements": list of strings (performance, security, etc.)
- "issues": list of objects, each with:
    - "title": action-phrase name for the coding task
    - "description": detailed description including which classes/functions to implement
    - "target_files": list of objects with "filepath" and "action" ("create" or "modify")
    - "test_files": list of test file paths (e.g. "tests/test_expense_manager.py")
    - "depends_on": list of issue titles this issue depends on (empty if none)
    - "suggested_model": the AI model to start with for this issue based on complexity:
        - "gpt-4o-mini" for simple tasks: data classes, enums, basic CRUD, simple utility functions, straightforward tests
        - "gpt-4o" for moderate tasks: business logic with multiple dependencies, CLI interfaces, modules with complex interactions
        - "gpt-5" for complex tasks: multi-module architectural work, complex algorithms, intricate state management
      Choose the cheapest model that can reliably solve the task. Most issues should use "gpt-4o-mini". Reserve "gpt-4o" for issues with 3+ dependencies or complex logic. Use "gpt-5" sparingly.

RULES FOR ISSUES:
- All file paths must be inside the package namespace or tests/ directory.
- Domain model issues come FIRST — they define shared types other issues depend on.
- An issue can create multiple files (e.g. a domain model file + its __init__.py update).
- test_files paths should start with "tests/".
- Order issues by dependency graph — if issue B depends on issue A, A comes first.
- depends_on references issues by their title string.
- Prefer cohesive issues: group related functionality together.
"""

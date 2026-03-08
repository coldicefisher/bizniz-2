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

RULES FOR ISSUES:
- All file paths must be inside the package namespace or tests/ directory.
- Domain model issues come FIRST — they define shared types other issues depend on.
- An issue can create multiple files (e.g. a domain model file + its __init__.py update).
- test_files paths should start with "tests/".
- Order issues by dependency graph — if issue B depends on issue A, A comes first.
- depends_on references issues by their title string.
- Prefer cohesive issues: group related functionality together.
"""

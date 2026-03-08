ANALYZE_PROMPT_TEMPLATE = """
Analyze the following problem statement and produce a complete engineering breakdown.

PROBLEM STATEMENT:
──────────────────────────────────────────────────────────────
{problem_statement}

Produce a JSON response with:
- "business_requirements": list of strings (business goals served)
- "use_cases": list of objects with "title" and "description"
- "functional_requirements": list of strings (concrete capabilities)
- "nonfunctional_requirements": list of strings (performance, security, etc.)
- "issues": list of objects, each with:
    - "title": action-phrase name for the coding task
    - "description": detailed description of what the module must do
    - "code_file": unique snake_case .py filename for the implementation
    - "test_file": unique snake_case .py filename for the tests (prefix with test_)

Each issue must represent exactly one self-contained Python module.
All code_file and test_file values must be unique across the list.
"""

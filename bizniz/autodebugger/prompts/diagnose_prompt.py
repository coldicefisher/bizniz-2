DIAGNOSE_PROMPT_TEMPLATE = """
A pytest run failed. Diagnose the root cause.

ERROR OUTPUT:
──────────────────────────────────────────────────────────────
{error_output}

CODE UNDER TEST ({code_filename}):
──────────────────────────────────────────────────────────────
{code}

TEST CODE ({test_filename}):
──────────────────────────────────────────────────────────────
{test_code}

WORKSPACE FILES:
──────────────────────────────────────────────────────────────
{workspace_files}

RELATED FILES (paths only):
──────────────────────────────────────────────────────────────
{related_files_listing}

Analyze the error, identify the root cause, and produce your diagnosis.
Related files are listed below. The diagnosis should focus on the code and test files provided.

Pay special attention to:
- Import errors (wrong module name, missing symbol, circular imports)
- Interface mismatches (tests expect a different function/class signature than the code provides)
- Dependency chains: if module A imports module B which imports module C, a bug in C can surface in A
- Package structure: check that __init__.py files export the right symbols
- Logic errors in the implementation vs what the tests expect
- Type errors (wrong argument types, missing return values)

When identifying relevant_files, include:
- Direct dependencies imported by the failing code
- Transitive dependencies (files imported by the direct dependencies)
- __init__.py files that re-export symbols used by the code
- Any file mentioned in the traceback

Use every detail in the traceback.

Return ONLY valid JSON matching the schema. No markdown, no code fences.
"""

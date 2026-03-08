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

{related_file_contents}

Analyze the error, identify the root cause, and produce your diagnosis.

Pay special attention to:
- Import errors (wrong module name, missing symbol, circular imports)
- Interface mismatches (tests expect a different function/class signature than the code provides)
- Missing dependencies on other workspace modules — check the RELATED FILE CONTENTS above
- Dependency chains: if module A imports module B which imports module C, a bug in C can surface in A
- Package structure: check that __init__.py files export the right symbols
- Logic errors in the implementation vs what the tests expect
- Type errors (wrong argument types, missing return values)

When identifying relevant_files, include:
- Direct dependencies imported by the failing code
- Transitive dependencies (files imported by the direct dependencies)
- __init__.py files that re-export symbols used by the code
- Any file mentioned in the traceback

The FULL untruncated error output is provided above. Use every detail in the traceback.

Return ONLY valid JSON matching the schema. No markdown, no code fences.
"""

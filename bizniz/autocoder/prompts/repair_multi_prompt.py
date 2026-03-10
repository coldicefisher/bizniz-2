REPAIR_MULTI_PROMPT_TEMPLATE = """
The project code failed with errors. You must fix the code across one or more files
to address the error and produce valid code that meets the original requirements.

IMPORTANT CONTEXT:
- Your code is being executed and tested automatically with pytest.
- Read the error output and test code carefully to understand what is expected.
- Your code must define the exact function/class signatures that tests import and call.
- Respect the package structure — use relative imports within the package.
- Do NOT import modules that don't exist in the workspace. Check the file list below.
- If a function or class is missing, create it — don't import it from a non-existent module.

COMMON MISTAKES TO AVOID:
- Importing from modules that don't exist (e.g. utils.validation when no utils/ directory exists)
- Wrong function signatures or return types that don't match what tests expect
- Forgetting to export classes/functions in __init__.py
- Using absolute imports when relative imports are needed (or vice versa)

Perform these steps carefully:
1. Read the error output and any test code carefully.
2. Identify the root cause — is it a logic error, missing function, wrong signature,
   import issue, or missing dependency between modules?
3. Check the WORKSPACE FILES list to verify all imports reference existing modules.
4. Determine which files need changes.
5. Return the COMPLETE content for every file you change.

WORKSPACE FILES:
──────────────────────────────────────────────────────────────
{workspace_files}

ARCHITECTURE CONTEXT:
──────────────────────────────────────────────────────────────
{architecture_context}

ERROR OUTPUT:
──────────────────────────────────────────────────────────────
Note: The error output may include a DEEP DIAGNOSIS section with a comprehensive
root cause analysis and fix plan from a separate debugging agent. If present,
follow the fix plan steps carefully — they are based on analysis of the full
project context and repair history.

{error_message}

CURRENT FILES:
──────────────────────────────────────────────────────────────
{current_files}

RESPONSE FORMAT:
──────────────────────────────────────────────────────────────
Return ONLY valid JSON:

{{
    "analysis": "<analysis of what went wrong>",
    "fix_plan": "<description of the minimal fix across files>",
    "changes": [
        {{
            "filepath": "pkg/module.py",
            "code": "<complete corrected file content>",
            "action": "modify"
        }}
    ],
    "dependencies": ["fastapi", "pydantic"]
}}

Each change must include the COMPLETE file content, not a diff.
Only include files that actually need changes.

The "dependencies" array must list ALL third-party packages your code imports.
Do NOT include standard library modules. Include the pip-installable package name.
Return an empty array if no third-party packages are needed.
"""

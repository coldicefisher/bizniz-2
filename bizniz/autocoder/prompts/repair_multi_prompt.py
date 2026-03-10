REPAIR_MULTI_PROMPT_TEMPLATE = """
The project code failed with errors. You must fix the code across one or more files
to address the error and produce valid code that meets the original requirements.

IMPORTANT CONTEXT:
- Your code is being executed and tested automatically with pytest.
- Read the error output and test code carefully to understand what is expected.
- Your code must define the exact function/class signatures that tests import and call.
- Respect the package structure — use relative imports within the package.

Perform these steps carefully:
1. Read the error output and any test code carefully.
2. Identify the root cause — is it a logic error, missing function, wrong signature,
   import issue, or missing dependency between modules?
3. Determine which files need changes.
4. Return the COMPLETE content for every file you change.

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

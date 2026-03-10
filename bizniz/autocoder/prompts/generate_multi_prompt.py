_GENERATE_MULTI_SYSTEM_PROMPT_PYTHON = """
You are an expert Python programmer working on a multi-file Python project.
You will be given a coding task along with architectural context and existing code,
and you must produce changes across one or more files.

INSTRUCTIONS:
──────────────────────────────────────────────────────────────
You will receive:
- A problem description / issue to implement
- Architecture context (package structure, domain models, dependencies)
- Existing code from related files in the project
- A list of target files you are expected to create or modify

You must return a JSON object with a "changes" array. Each element describes one
file to create, modify, or delete.

RULES:
- Return the COMPLETE content for every file you touch — no partial snippets.
- Respect the architecture plan: use the prescribed namespaces, class names, and
  module structure. Do NOT invent new modules or classes outside the plan.
- Use relative imports within the package (e.g. `from .models import Expense`).
- Ensure all `__init__.py` files export the public API of their package.
- Write clean, production-quality Python with type hints.
- Do NOT include test code in source files.

EVALUATION ENVIRONMENT
──────────────────────────────────────────────────────────────
{evaluation_environment}
"""

_GENERATE_MULTI_SYSTEM_PROMPT_TYPESCRIPT = """
You are an expert TypeScript/React programmer working on a multi-file TypeScript project.
You will be given a coding task along with architectural context and existing code,
and you must produce changes across one or more files.

INSTRUCTIONS:
──────────────────────────────────────────────────────────────
You will receive:
- A problem description / issue to implement
- Architecture context (package structure, domain models, dependencies)
- Existing code from related files in the project
- A list of target files you are expected to create or modify

You must return a JSON object with a "changes" array. Each element describes one
file to create, modify, or delete.

RULES:
- Return the COMPLETE content for every file you touch — no partial snippets.
- Respect the architecture plan: use the prescribed namespaces, class names, and
  module structure. Do NOT invent new modules or classes outside the plan.
- Use standard ES module imports (e.g. `import {{ Expense }} from './models'`).
- All files must use .ts or .tsx extensions (tsx for React components).
- Write clean, production-quality TypeScript with proper type annotations.
- Do NOT include test code in source files.

EVALUATION ENVIRONMENT
──────────────────────────────────────────────────────────────
{evaluation_environment}
"""


def get_generate_multi_system_prompt(language: str = "python") -> str:
    if language == "typescript":
        return _GENERATE_MULTI_SYSTEM_PROMPT_TYPESCRIPT
    return _GENERATE_MULTI_SYSTEM_PROMPT_PYTHON


# Backward compatibility
GENERATE_MULTI_SYSTEM_PROMPT = _GENERATE_MULTI_SYSTEM_PROMPT_PYTHON


GENERATE_MULTI_USER_PROMPT_TEMPLATE = """
PROJECT ROOT:
──────────────────────────────────────────────────────────────
{project_root}

ISSUE:
──────────────────────────────────────────────────────────────
{issue_description}

ARCHITECTURE CONTEXT:
──────────────────────────────────────────────────────────────
{architecture_context}

TARGET FILES:
──────────────────────────────────────────────────────────────
{target_files_description}

EXISTING CODE:
──────────────────────────────────────────────────────────────
{existing_code}

RESPONSE FORMAT:
──────────────────────────────────────────────────────────────
Return ONLY valid JSON with a "changes" array and a "dependencies" array:

{{
    "changes": [
        {{
            "filepath": "pkg/module.py",
            "code": "<complete file content>",
            "action": "create"
        }},
        {{
            "filepath": "pkg/__init__.py",
            "code": "<complete file content>",
            "action": "modify"
        }}
    ],
    "dependencies": ["fastapi", "pydantic", "httpx"]
}}

Each change must include:
- filepath: workspace-relative path
- code: the COMPLETE file content (not a diff)
- action: "create", "modify", or "delete"

The "dependencies" array must list ALL third-party packages your code imports.
Do NOT include standard library modules (os, sys, json, etc.).
Include the pip-installable package name (e.g. "pydantic" not "pydantic.BaseModel").
Return an empty array if no third-party packages are needed.
"""

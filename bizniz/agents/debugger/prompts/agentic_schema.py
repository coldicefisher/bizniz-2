"""
JSON schema for the AgenticDebugger action responses.

The LLM returns one of these actions per turn. See the system prompt
for full descriptions of each action and which fields apply.
"""

AgenticDebuggerActionSchema = {
    "name": "debugger_action",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "thinking": {
                "type": "string",
                "description": "Your reasoning about the current state and what to do next.",
            },
            "action": {
                "type": "string",
                "enum": [
                    # Static workspace inspection
                    "view_file",
                    "list_directory",
                    "search_files",
                    "search_imports",
                    "list_all_imports",
                    # Host-side commands
                    "run_command",
                    "run_tests",
                    # Live container introspection
                    "tail_logs",
                    "run_in_container",
                    "run_python_in_container",
                    "hit_endpoint",
                    "inspect_env",
                    "query_database",
                    # Utility
                    "decode_jwt",
                    # Legacy combined tool — prefer the discrete ones above
                    "inspect_container",
                    # Terminal
                    "submit_fix",
                ],
                "description": "The action to take.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Primary string argument. Meaning depends on action: "
                    "view_file → file path; list_directory → directory path; "
                    "search_files → regex pattern; search_imports → symbol; "
                    "list_all_imports → module path; run_command → shell command; "
                    "run_tests → test paths; tail_logs → number of lines (e.g. '200'); "
                    "inspect_env → env var prefix (e.g. 'FUSIONAUTH'); "
                    "decode_jwt → leave empty (use 'token' field); "
                    "hit_endpoint, run_in_container, run_python_in_container, "
                    "query_database → leave empty (use the dedicated fields); "
                    "submit_fix → leave empty."
                ),
            },
            "service": {
                "type": "string",
                "description": (
                    "Optional target container service name. "
                    "Used by tail_logs, run_in_container, run_python_in_container, "
                    "inspect_env, query_database, hit_endpoint. "
                    "Empty string means: use the service this debugger is bound to "
                    "(usually the failing backend). For query_database, leave empty "
                    "to auto-target the project's postgres service. "
                    "Use empty string for actions that don't target a container."
                ),
            },
            "command": {
                "type": "string",
                "description": (
                    "Command body for container-execution actions. "
                    "For run_in_container: shell command (e.g. 'printenv | sort'). "
                    "For run_python_in_container: Python code, will run via "
                    "`python -c '<code>'` (e.g. 'from app.core.config import get_settings; "
                    "print(get_settings().fusionauth_issuer)'). "
                    "For query_database: a SQL statement (SELECT preferred). "
                    "Use empty string for actions that don't run a command."
                ),
            },
            "url": {
                "type": "string",
                "description": (
                    "HTTP URL for hit_endpoint. Use the docker-network "
                    "hostname (e.g. 'http://backend:8000/api/v1/auth/me' or "
                    "'http://auth:9011/api/login'), NOT 'localhost', because "
                    "the request runs from inside a container. "
                    "Use empty string for non-HTTP actions."
                ),
            },
            "request_data": {
                "type": "string",
                "description": (
                    "JSON-encoded object for hit_endpoint with optional keys: "
                    "{\"method\": \"POST\", \"headers\": {\"Authorization\": \"Bearer ...\"}, "
                    "\"body\": {...}}. "
                    "Method defaults to GET if omitted. Body may be an object (sent as JSON) "
                    "or a string (sent as raw text). "
                    "Use '{}' or empty string for GET requests with no headers/body, or for "
                    "non-HTTP actions."
                ),
            },
            "token": {
                "type": "string",
                "description": (
                    "JWT string for decode_jwt. The tool will return the decoded "
                    "header + payload (signature is NOT verified — debug-only). "
                    "Use empty string for non-JWT actions."
                ),
            },
            "fix_target": {
                "type": "string",
                "enum": ["code", "tests", "both"],
                "description": "What should be fixed (only used with submit_fix).",
            },
            "diagnosis": {
                "type": "string",
                "description": "Root cause explanation (only used with submit_fix).",
            },
            "root_cause_category": {
                "type": "string",
                "enum": [
                    "logic_error",
                    "interface_mismatch",
                    "missing_implementation",
                    "dependency_issue",
                    "architectural_flaw",
                    "test_issue",
                    "import_error",
                    "other",
                ],
                "description": "Category of the root cause (only used with submit_fix).",
            },
            "fix_plan": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered list of fix steps (only used with submit_fix).",
            },
            "suggested_approach": {
                "type": "string",
                "description": "How to approach the fix (only used with submit_fix).",
            },
            "missing_packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Missing pip packages to install (only used with submit_fix).",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Confidence in the diagnosis (only used with submit_fix).",
            },
            "code_fixes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string"},
                        "new_content": {"type": "string"},
                    },
                    "required": ["filepath", "new_content"],
                    "additionalProperties": False,
                },
                "description": "Direct code fixes to apply (only used with submit_fix). Each entry is a file to write.",
            },
        },
        "required": [
            "thinking",
            "action",
            "path",
            "service",
            "command",
            "url",
            "request_data",
            "token",
            "fix_target",
            "diagnosis",
            "root_cause_category",
            "fix_plan",
            "suggested_approach",
            "missing_packages",
            "confidence",
            "code_fixes",
        ],
        "additionalProperties": False,
    },
}

"""Action schema for Coder's tool loop.

Same shape as Engineer's (one big enum + flat optional payload
fields) but with a ``validate_symbols`` action and no plan/issue
actions (Coder works on ONE issue, no planning needed).
"""
from __future__ import annotations


_ALLOWED_ACTIONS = [
    # discovery
    "view_file",
    "list_directory",
    "search_files",
    "get_file_outline",
    "get_workspace_tree",
    "list_routes",
    "list_dependencies",
    "list_pydantic_models",
    "search_imports",
    "list_all_imports",
    # mutation
    "write_file",
    # validation (the v2.5-new piece)
    "validate_symbols",
    # tests + smoke
    "run_tests",
    "smoke_import",
    # container introspection
    "run_in_container",
    "run_python_in_container",
    "hit_endpoint",
    "inspect_env",
    "tail_logs",
    "query_database",
    "decode_jwt",
    # terminal
    "submit_code",
]


CODER_ACTION_SCHEMA = {
    "name": "CoderAction",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "thinking",
            "action",
            "path",
            "new_content",
            "query",
            "service",
            "url",
            "request_data",
            "command",
            "sql",
            "token",
            "summary",
            "status",
            "notes",
        ],
        "properties": {
            "thinking": {
                "type": "string",
                "description": "Scratchpad — keep under ~200 words.",
            },
            "action": {
                "type": "string",
                "enum": _ALLOWED_ACTIONS,
            },
            "path": {
                "type": "string",
                "description": (
                    "view_file / list_directory / write_file / "
                    "smoke_import / get_file_outline / inspect_env / "
                    "tail_logs path-or-prefix-or-tail-count. "
                    "validate_symbols ignores this (validates all "
                    "target_files written so far)."
                ),
            },
            "new_content": {
                "type": "string",
                "description": "write_file content.",
            },
            "query": {
                "type": "string",
                "description": "search_files / search_imports query.",
            },
            "service": {
                "type": "string",
                "description": (
                    "Service override for run_*, hit_endpoint, "
                    "inspect_env, tail_logs, query_database, "
                    "smoke_import, run_tests."
                ),
            },
            "url": {
                "type": "string",
                "description": "hit_endpoint URL.",
            },
            "request_data": {
                "type": "string",
                "description": "hit_endpoint extras as JSON-as-string.",
            },
            "command": {
                "type": "string",
                "description": (
                    "Shell for run_in_container, "
                    "Python source for run_python_in_container."
                ),
            },
            "sql": {
                "type": "string",
                "description": "query_database SQL.",
            },
            "token": {
                "type": "string",
                "description": "decode_jwt token.",
            },
            "summary": {
                "type": "string",
                "description": "submit_code summary.",
            },
            "status": {
                "type": "string",
                "enum": ["passed", "partial", "deferred", "failed"],
                "description": "submit_code: terminal status.",
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "submit_code: free-text observations.",
            },
        },
    },
}

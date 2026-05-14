"""Action schema for the Engineer's tool loop.

One large schema with an ``action`` enum covering every action the
Engineer can emit. Each turn the LLM picks one action and fills the
relevant fields. Unused fields must be present with empty values
(empty string or empty list); the schema marks them required so the
LLM can't omit them.
"""
from __future__ import annotations


_ALLOWED_ACTIONS = [
    # plan / status
    "submit_plan",
    "revise_plan",
    "get_my_plan",
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
    "submit_implementation",
]


_ISSUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "title",
        "description",
        "target_files",
        "test_files",
        "success_criteria",
        "spec_refs",
        "depends_on",
    ],
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "target_files": {"type": "array", "items": {"type": "string"}},
        "test_files": {"type": "array", "items": {"type": "string"}},
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        "spec_refs": {"type": "array", "items": {"type": "string"}},
        "depends_on": {"type": "array", "items": {"type": "string"}},
    },
}


ENGINEER_ACTION_SCHEMA = {
    "name": "EngineerAction",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "thinking",
            "action",
            # plan/revise fields
            "approach",
            "issues",
            # mutation
            "path",
            "new_content",
            # query
            "query",
            "service",
            "url",
            "request_data",
            "command",
            "sql",
            "token",
            # terminal payload
            "summary",
            "final_test_status",
            "completed_issue_ids",
            "deferred_issue_ids",
            "notes",
        ],
        "properties": {
            "thinking": {
                "type": "string",
                "description": "Scratchpad for reasoning. Keep under ~200 words.",
            },
            "action": {
                "type": "string",
                "enum": _ALLOWED_ACTIONS,
            },
            "approach": {
                "type": "string",
                "description": "Used by submit_plan / revise_plan only.",
            },
            "issues": {
                "type": "array",
                "items": _ISSUE_SCHEMA,
                "description": "Used by submit_plan / revise_plan only.",
            },
            "path": {
                "type": "string",
                "description": (
                    "view_file / list_directory / write_file / "
                    "smoke_import / get_file_outline / inspect_env / "
                    "tail_logs path-or-prefix-or-tail-count."
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
                    "Container service name override for run_*, "
                    "hit_endpoint, inspect_env, tail_logs, query_database."
                ),
            },
            "url": {
                "type": "string",
                "description": "hit_endpoint URL.",
            },
            "request_data": {
                "type": "string",
                "description": (
                    "hit_endpoint extras as JSON-as-string: "
                    "{method, headers, body}."
                ),
            },
            "command": {
                "type": "string",
                "description": (
                    "Shell command for run_in_container, "
                    "Python source for run_python_in_container."
                ),
            },
            "sql": {
                "type": "string",
                "description": "query_database SQL.",
            },
            "token": {
                "type": "string",
                "description": "decode_jwt token (with or without 'Bearer ').",
            },
            "summary": {
                "type": "string",
                "description": "submit_implementation: 2-5 sentence summary.",
            },
            "final_test_status": {
                "type": "string",
                "enum": ["passed", "partial", "failed", "not_run"],
                "description": "submit_implementation: overall test status.",
            },
            "completed_issue_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "deferred_issue_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "submit_implementation: free-text observations the "
                    "reviewer should know about."
                ),
            },
        },
    },
}

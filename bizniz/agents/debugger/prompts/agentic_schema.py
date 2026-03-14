"""
JSON schema for the AgenticDebugger action responses.

The LLM returns one of these actions per turn:
- view_file: Read a file from the workspace
- list_directory: List files in a directory
- run_tests: Execute pytest on specific test files
- submit_fix: Submit the final diagnosis and optional code fixes
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
                "enum": ["view_file", "list_directory", "search_files", "run_command", "run_tests", "submit_fix"],
                "description": "The action to take.",
            },
            "path": {
                "type": "string",
                "description": "For view_file: file path. For list_directory: directory path. For search_files: regex pattern. For run_command: the shell command. For run_tests: space-separated test file paths. For submit_fix: leave empty.",
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

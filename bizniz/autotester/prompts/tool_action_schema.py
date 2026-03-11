"""
Tool-action schema for autotester.

Used by generate_multi when running in agentic mode with discovery tools.
"""

from bizniz.tools.schemas import build_tool_action_schema


AutotesterGenerateActionSchema = build_tool_action_schema(
    name="autotester_generate_action",
    terminal_action="submit_tests",
    terminal_properties={
        "test_files": {
            "type": "array",
            "description": "List of test files to create. Empty array if using a discovery tool.",
            "items": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Test file path relative to workspace root",
                    },
                    "tests": {
                        "type": "string",
                        "description": "Complete pytest test file content",
                    },
                },
                "required": ["filepath", "tests"],
                "additionalProperties": False,
            },
        },
        "notes": {
            "type": "string",
            "description": "Brief description of what the tests cover. Empty string if using a discovery tool.",
        },
        "dependencies": {
            "type": "array",
            "description": "Third-party test packages required. Empty array if using a discovery tool.",
            "items": {"type": "string"},
        },
    },
    terminal_required=["test_files", "notes", "dependencies"],
)

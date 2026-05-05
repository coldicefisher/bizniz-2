"""
Tool-action schemas for coder.

Used by generate_multi and repair_multi when running in agentic mode
with discovery tools.
"""

from bizniz.tools.schemas import build_tool_action_schema


_CHANGES_PROPERTY = {
    "changes": {
        "type": "array",
        "description": "File changes to create or modify. Use empty array [] for discovery actions; MUST be non-empty when action is submit_code.",
        "items": {
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "File path relative to workspace root",
                },
                "code": {
                    "type": "string",
                    "description": "Complete file content",
                },
                "action": {
                    "type": "string",
                    "enum": ["create", "modify", "delete"],
                },
            },
            "required": ["filepath", "code", "action"],
            "additionalProperties": False,
        },
    },
    "dependencies": {
        "type": "array",
        "description": "Third-party pip packages your code imports. Empty array if none needed.",
        "items": {"type": "string"},
    },
}

_TEST_SCAFFOLD_PROPERTY = {
    "test_scaffold": {
        "type": "string",
        "description": "A minimal test file scaffold showing correct imports, client setup, and one example test. This helps the test writer understand how to test this code. Include the correct import paths for your code, any test client setup (e.g. FastAPI TestClient, Express supertest), and one example test case. Empty string if not applicable.",
    },
}


CoderGenerateActionSchema = build_tool_action_schema(
    name="autocoder_generate_action",
    terminal_action="submit_code",
    terminal_properties={**_CHANGES_PROPERTY, **_TEST_SCAFFOLD_PROPERTY},
    terminal_required=["changes", "dependencies", "test_scaffold"],
)


CoderRepairActionSchema = build_tool_action_schema(
    name="autocoder_repair_action",
    terminal_action="submit_code",
    terminal_properties={
        "analysis": {
            "type": "string",
            "description": "Analysis of what went wrong. Empty string if using a discovery tool.",
        },
        "fix_plan": {
            "type": "string",
            "description": "Step-by-step plan for the fix. Empty string if using a discovery tool.",
        },
        **_CHANGES_PROPERTY,
    },
    terminal_required=["analysis", "fix_plan", "changes", "dependencies"],
)

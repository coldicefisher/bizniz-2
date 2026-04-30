AutodebuggerSchema = {
    "name": "autodebugger_diagnosis",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "diagnosis": {
                "type": "string",
                "description": "Clear explanation of the root cause of the failure.",
            },
            "fix_target": {
                "type": "string",
                "enum": ["code", "tests"],
                "description": "Whether the code or the tests need to be fixed.",
            },
            "relevant_files": {
                "type": "array",
                "description": "List of workspace files relevant to the diagnosis.",
                "items": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "Workspace-relative filename.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "What this file provides or why it is relevant.",
                        },
                    },
                    "required": ["filename", "summary"],
                    "additionalProperties": False,
                },
            },
            "suggested_approach": {
                "type": "string",
                "description": "Specific, actionable steps for the repair agent.",
            },
            "affected_files": {
                "type": "array",
                "description": "File paths that should be modified to fix the issue.",
                "items": {"type": "string"},
            },
        },
        "required": ["diagnosis", "fix_target", "relevant_files", "suggested_approach", "affected_files"],
        "additionalProperties": False,
    },
}

"""JSON schema for ServicePlanner's structured output."""

SERVICE_PLANNER_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "service_plan",
        "schema": {
            "type": "object",
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Stable id, e.g. 'BE-001'.",
                            },
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "target_files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Workspace-relative paths (1-2 files).",
                            },
                            "test_files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "DEDICATED test file(s) for this issue.",
                            },
                            "success_criteria": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "spec_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Capability ids from the EnrichedSpec.",
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Other issue ids in THIS service.",
                            },
                        },
                        "required": [
                            "id", "title", "description", "target_files",
                            "test_files", "spec_refs", "depends_on",
                            "success_criteria",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["issues"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

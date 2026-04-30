PlannerSchema = {
    "name": "project_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "required": [
            "project_name",
            "project_slug",
            "description",
            "milestones",
        ],
        "properties": {
            "project_name": {"type": "string"},
            "project_slug": {"type": "string"},
            "description": {
                "type": "string",
                "description": "1-2 sentence overview of the plan",
            },
            "milestones": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "sequence_index",
                        "name",
                        "problem_slice",
                        "use_cases",
                        "success_criteria",
                        "depends_on_names",
                        "estimated_effort",
                    ],
                    "properties": {
                        "sequence_index": {
                            "type": "integer",
                            "description": "0-based ordering",
                        },
                        "name": {
                            "type": "string",
                            "description": "Short label (3-6 words)",
                        },
                        "problem_slice": {
                            "type": "string",
                            "description": (
                                "Self-contained problem statement for just "
                                "this milestone. The Architect will read this "
                                "standalone and decompose it into services."
                            ),
                        },
                        "use_cases": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "User stories: 'user can ...'",
                        },
                        "success_criteria": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Testable outcomes from a user's perspective"
                            ),
                        },
                        "depends_on_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Names of other milestones in this plan that "
                                "must ship before this one"
                            ),
                        },
                        "estimated_effort": {
                            "type": "string",
                            "enum": ["S", "M", "L"],
                            "description": "S=days, M=~week, L=1-2 weeks",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    },
}

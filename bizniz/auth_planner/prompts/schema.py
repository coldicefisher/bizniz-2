"""JSON schema for AuthPlanner's structured output. Mirrors the
``AuthSpec`` Pydantic model in ``bizniz/auth_orchestrators/spec.py``
but flattened to what the LLM needs to emit (no deltas, no soft-
delete bookkeeping)."""

AUTH_PLANNER_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "auth_spec",
        "schema": {
            "type": "object",
            "properties": {
                "enable_auth": {"type": "boolean"},
                "enable_groups": {"type": "boolean"},
                "enable_multitenant": {"type": "boolean"},
                "roles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "is_super_role": {"type": "boolean"},
                        },
                        "required": ["name", "description", "is_super_role"],
                        "additionalProperties": False,
                    },
                },
                "applications": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "role_names": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Roles registered on this "
                                               "app. Empty = all spec roles.",
                            },
                        },
                        "required": ["name", "role_names"],
                        "additionalProperties": False,
                    },
                },
                "test_users": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "email": {"type": "string"},
                            "password": {"type": "string"},
                            "first_name": {"type": "string"},
                            "last_name": {"type": "string"},
                            "role_names": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "email", "password", "first_name",
                            "last_name", "role_names",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "enable_auth", "enable_groups", "enable_multitenant",
                "roles", "applications", "test_users",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

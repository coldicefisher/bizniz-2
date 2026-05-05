# JSON schema for the planner's structured output. Mirrors
# ``bizniz/planner/types.py`` (Milestone) and ``bizniz/auth/spec.py``
# (AuthSpecDelta) — keep these in sync when fields change.

# AuthSpecDelta nested schema. Lives here (not in bizniz/auth/) because
# it's the LLM contract specifically; the Pydantic model is the runtime
# contract. They share field names but not strictness — the LLM schema
# is permissive and we let Pydantic do final validation.
_role_spec_schema = {
    "type": "object",
    "required": ["name", "description", "is_default"],
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "is_default": {"type": "boolean"},
    },
    "additionalProperties": False,
}

_app_spec_schema = {
    "type": "object",
    "required": ["name", "redirect_urls", "pkce_required"],
    "properties": {
        "name": {"type": "string"},
        "redirect_urls": {"type": "array", "items": {"type": "string"}},
        "pkce_required": {"type": "boolean"},
    },
    "additionalProperties": False,
}

_group_spec_schema = {
    "type": "object",
    "required": ["name", "description", "application", "role_names"],
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "application": {"type": "string"},
        "role_names": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

_user_spec_schema = {
    "type": "object",
    "required": ["email", "first_name", "last_name", "role_names", "group_names"],
    "properties": {
        "email": {"type": "string"},
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "role_names": {"type": "array", "items": {"type": "string"}},
        "group_names": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

_auth_delta_schema = {
    "type": "object",
    "description": (
        "Auth state changes introduced by this milestone. Empty / "
        "default values when the milestone doesn't touch auth."
    ),
    "required": [
        "enable_auth",
        "enable_groups",
        "enable_multitenant",
        "add_roles",
        "remove_roles",
        "add_applications",
        "add_groups",
        "add_test_users",
        "note",
    ],
    "properties": {
        "enable_auth": {
            "type": "boolean",
            "description": "Set true on the M1 auth milestone. False/unchanged otherwise.",
        },
        "enable_groups": {"type": "boolean"},
        "enable_multitenant": {"type": "boolean"},
        "add_roles": {"type": "array", "items": _role_spec_schema},
        "remove_roles": {"type": "array", "items": {"type": "string"}},
        "add_applications": {"type": "array", "items": _app_spec_schema},
        "add_groups": {"type": "array", "items": _group_spec_schema},
        "add_test_users": {"type": "array", "items": _user_spec_schema},
        "note": {"type": "string"},
    },
    "additionalProperties": False,
}


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
                        "auth_delta",
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
                        "auth_delta": _auth_delta_schema,
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    },
}

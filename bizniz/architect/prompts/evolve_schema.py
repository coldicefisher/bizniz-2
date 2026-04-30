"""JSON schema for Architect.evolve responses.

Mirrors AutoArchitectSchema but adds the required ``evolve_state`` field
on each service.
"""
EvolveArchitectSchema = {
    "name": "evolved_architecture",
    "strict": True,
    "schema": {
        "type": "object",
        "required": [
            "project_name",
            "project_slug",
            "description",
            "services",
        ],
        "properties": {
            "project_name": {"type": "string"},
            "project_slug": {"type": "string"},
            "description": {"type": "string"},
            "services": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "name",
                        "service_type",
                        "framework",
                        "language",
                        "description",
                        "workspace_name",
                        "port",
                        "depends_on",
                        "requirements",
                        "skeleton",
                        "evolve_state",
                    ],
                    "properties": {
                        "name": {"type": "string"},
                        "service_type": {
                            "type": "string",
                            "enum": [
                                "backend", "frontend", "database",
                                "cache", "proxy", "worker", "auth",
                            ],
                        },
                        "framework": {"type": "string"},
                        "language": {"type": "string"},
                        "description": {"type": "string"},
                        "workspace_name": {"type": "string"},
                        "port": {"type": ["integer", "null"]},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "requirements": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "skeleton": {
                            "type": "string",
                            "enum": [
                                "fastapi", "react", "angular",
                                "teams-backend", "teams-consumer", "teams-frontend",
                                "none",
                            ],
                        },
                        "evolve_state": {
                            "type": "string",
                            "enum": ["new", "extended", "unchanged"],
                            "description": (
                                "How this service relates to the existing "
                                "architecture: new (added for this milestone), "
                                "extended (existed but milestone adds to it), "
                                "or unchanged (milestone doesn't touch it)."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    },
}

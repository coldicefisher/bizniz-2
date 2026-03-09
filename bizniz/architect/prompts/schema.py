AutoArchitectSchema = {
    "name": "system_architecture",
    "strict": True,
    "schema": {
        "type": "object",
        "required": [
            "project_name",
            "project_slug",
            "description",
            "services",
            "docker_compose",
        ],
        "properties": {
            "project_name": {
                "type": "string",
                "description": "Human-readable project name",
            },
            "project_slug": {
                "type": "string",
                "description": "Slugified project name (e.g. pet_groomer)",
            },
            "description": {
                "type": "string",
                "description": "Overall system description",
            },
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
                            "description": "List of packages to install (pip for Python, npm for TypeScript)",
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "docker_compose": {
                "type": "string",
                "description": "Complete docker-compose.yml content",
            },
        },
        "additionalProperties": False,
    },
}

GeneratePromptSchema = {
    "type": "json_schema",
    "json_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string"
            }
        },
        "required": ["code"]
    }
}

RepairPromptSchema = {
    "type": "json_schema",
    "json_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string"
            },
            "analysis": {
                "type": "string"
            },
            "fix_plan": {
                "type": "string"
            }
        },
        "required": ["code", "analysis", "fix_plan"]
    }
}

VerificationPromptSchema = {
    "type": "json_schema",
    "json_schema": {
        "type": "object",
        "properties": {
            "is_valid": {
                "type": "boolean"
            },
            "errors": {
                "type": "array",
                "items": {
                    "type": "string"
                }
            },
            "code": {
                "type": "string"
            }
        },
        "required": ["is_valid", "errors", "code"]
    }
}
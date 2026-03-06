GeneratePromptSchema = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string"
        }
    },
    "required": ["code"],
    "additionalProperties": False
}

RepairPromptSchema = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "analysis": {"type": "string"},
        "fix_plan": {"type": "string"}
    },
    "required": ["code", "analysis", "fix_plan"],
    "additionalProperties": False
}

VerificationPromptSchema = {
    "type": "object",
    "properties": {
        "is_valid": {"type": "boolean"},
        "errors": {
            "type": "array",
            "items": {"type": "string"}
        },
        "code": {"type": "string"}
    },
    "required": ["is_valid", "errors", "code"],
    "additionalProperties": False
}
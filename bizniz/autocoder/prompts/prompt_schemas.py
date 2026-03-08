GeneratePromptSchema = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "call_spec": {"type": "string"},
    },
    "required": ["code", "call_spec"],
    "additionalProperties": False,
}

RepairPromptSchema = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "analysis": {"type": "string"},
        "fix_plan": {"type": "string"},
        "call_spec": {"type": "string"},
    },
    "required": ["code", "analysis", "fix_plan", "call_spec"],
    "additionalProperties": False,
}


VerificationPromptSchema = {
    "type": "object",
    "properties": {
        "is_valid": {"type": "boolean"},
        "errors": {
            "type": "array",
            "items": {"type": "string"},
        },
        "code": {"type": "string"},
        "call_spec": {"type": "string"},
    },
    "required": ["is_valid", "errors", "code", "call_spec"],
    "additionalProperties": False,
}

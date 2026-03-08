GeneratePromptSchema = {
    "name": "generate_code",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "description": "List of file changes to create or modify",
                "items": {
                    "type": "object",
                    "properties": {
                        "filepath": {
                            "type": "string",
                            "description": "File path relative to workspace root"
                        },
                        "code": {
                            "type": "string",
                            "description": "Complete file content"
                        },
                        "action": {
                            "type": "string",
                            "enum": ["create", "modify", "delete"]
                        }
                    },
                    "required": ["filepath", "code", "action"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["changes"],
        "additionalProperties": False,
    }
}


RepairPromptSchema = {
    "name": "repair_code",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "analysis": {
                "type": "string",
                "description": "Analysis of what went wrong"
            },
            "fix_plan": {
                "type": "string",
                "description": "Step-by-step plan for the fix"
            },
            "changes": {
                "type": "array",
                "description": "List of file changes to fix the issue",
                "items": {
                    "type": "object",
                    "properties": {
                        "filepath": {
                            "type": "string",
                            "description": "File path relative to workspace root"
                        },
                        "code": {
                            "type": "string",
                            "description": "Complete file content"
                        },
                        "action": {
                            "type": "string",
                            "enum": ["create", "modify", "delete"]
                        }
                    },
                    "required": ["filepath", "code", "action"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["analysis", "fix_plan", "changes"],
        "additionalProperties": False,
    }
}


VerificationPromptSchema = {
    "name": "verify_code",
    "strict": True,
    "schema": {
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
}

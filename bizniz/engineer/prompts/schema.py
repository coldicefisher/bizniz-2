AutoEngineerSchema = {
    "type": "object",
    "properties": {
        "business_requirements": {
            "type": "array",
            "items": {"type": "string"}
        },
        "use_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"}
                },
                "required": ["title", "description"],
                "additionalProperties": False
            }
        },
        "functional_requirements": {
            "type": "array",
            "items": {"type": "string"}
        },
        "nonfunctional_requirements": {
            "type": "array",
            "items": {"type": "string"}
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "code_file": {"type": "string"},
                    "test_file": {"type": "string"}
                },
                "required": ["title", "description", "code_file", "test_file"],
                "additionalProperties": False
            }
        }
    },
    "required": [
        "business_requirements",
        "use_cases",
        "functional_requirements",
        "nonfunctional_requirements",
        "issues"
    ],
    "additionalProperties": False
}

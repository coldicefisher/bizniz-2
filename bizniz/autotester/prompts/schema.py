AutotesterSchema = {
    "type": "object",
    "properties": {
        "tests": {
            "type": "string",
            "description": "Complete pytest test file as a Python source string."
        },
        "notes": {
            "type": "string",
            "description": "Brief description of what the tests cover."
        }
    },
    "required": ["tests"],
    "additionalProperties": False
}

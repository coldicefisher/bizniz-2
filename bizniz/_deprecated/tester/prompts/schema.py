TesterSchema = {
    "name": "generate_tests",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "test_files": {
                "type": "array",
                "description": "List of test files to create",
                "items": {
                    "type": "object",
                    "properties": {
                        "filepath": {
                            "type": "string",
                            "description": "Test file path relative to workspace root, e.g. 'tests/test_expense.py'"
                        },
                        "tests": {
                            "type": "string",
                            "description": "Complete pytest test file content as Python source"
                        }
                    },
                    "required": ["filepath", "tests"],
                    "additionalProperties": False
                }
            },
            "notes": {
                "type": "string",
                "description": "Brief description of what the tests cover"
            },
            "dependencies": {
                "type": "array",
                "description": "List of third-party test packages required (e.g. ['pytest', 'pytest-asyncio', 'httpx']). Do NOT include standard library modules.",
                "items": {"type": "string"}
            }
        },
        "required": ["test_files", "notes", "dependencies"],
        "additionalProperties": False
    }
}

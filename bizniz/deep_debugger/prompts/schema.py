DeepDiagnosisSchema = {
    "name": "deep_diagnosis",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "root_cause": {"type": "string"},
            "root_cause_category": {
                "type": "string",
                "enum": [
                    "logic_error", "interface_mismatch", "missing_implementation",
                    "dependency_issue", "architectural_flaw", "test_issue",
                ],
            },
            "fix_target": {
                "type": "string",
                "enum": ["code", "tests", "both"],
                "description": "Whether to fix the source code, the tests, or both.",
            },
            "affected_files": {"type": "array", "items": {"type": "string"}},
            "fix_plan": {"type": "array", "items": {"type": "string"}},
            "suggested_approach": {"type": "string"},
            "missing_packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "pip package names that need to be installed (empty if not a dependency issue).",
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "repair_history_analysis": {"type": "string"},
        },
        "required": [
            "root_cause", "root_cause_category", "fix_target", "affected_files",
            "fix_plan", "suggested_approach", "missing_packages", "confidence", "repair_history_analysis",
        ],
        "additionalProperties": False,
    },
}

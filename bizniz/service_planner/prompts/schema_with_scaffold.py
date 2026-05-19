"""ServicePlanner schema variant — adds ``seeded_files`` output.

The v3 pipeline spec (docs/architecture/v3_pipeline_spec.md) tests whether
ServicePlanner can ALSO emit a concrete seeded scaffold alongside the
issue specs. The scaffold gives Coder + Tester a shared contract: function
signatures, imports, type declarations, and route/handler stubs — with
bodies left unimplemented (``raise NotImplementedError`` or ``pass``).

Lives next to ``schema.py`` (production) so the test variant doesn't
disturb the live ServicePlanner. If validation passes, this schema
promotes into ``schema.py``.
"""

SERVICE_PLANNER_SCAFFOLD_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "service_plan_with_scaffold",
        "schema": {
            "type": "object",
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "target_files": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "test_files": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "success_criteria": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "spec_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "id", "title", "description", "target_files",
                            "test_files", "spec_refs", "depends_on",
                            "success_criteria",
                        ],
                        "additionalProperties": False,
                    },
                },
                "seeded_files": {
                    "type": "array",
                    "description": (
                        "Concrete scaffold of every file an issue's "
                        "target_files references. Each file's content must "
                        "be syntactically valid (parses cleanly), have all "
                        "imports + types + signatures + route registrations "
                        "in place, but leave business-logic bodies "
                        "unimplemented (raise NotImplementedError / pass)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": (
                                    "Workspace-relative path matching an "
                                    "issue's target_files entry (no service "
                                    "prefix; e.g. ``app/api/routes/recipes.py``)."
                                ),
                            },
                            "content": {
                                "type": "string",
                                "description": (
                                    "Complete file contents. Imports, "
                                    "function signatures, type/Pydantic "
                                    "class declarations, route/decorator "
                                    "registrations — all real. Bodies are "
                                    "stubs: ``raise NotImplementedError`` "
                                    "or ``pass``. NO inline business logic."
                                ),
                            },
                            "rationale": {
                                "type": "string",
                                "description": (
                                    "One-sentence note on what symbols this "
                                    "file exports and which issue(s) will "
                                    "fill the bodies."
                                ),
                            },
                        },
                        "required": ["path", "content", "rationale"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["issues", "seeded_files"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

# Schema for the initial analysis step (requirements, use cases, issues)
AutoEngineerSchema = {
    "name": "engineering_analysis",
    "strict": True,
    "schema": {
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
                        "target_files": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "filepath": {"type": "string"},
                                    "action": {
                                        "type": "string",
                                        "enum": ["create", "modify", "delete"]
                                    }
                                },
                                "required": ["filepath", "action"],
                                "additionalProperties": False
                            }
                        },
                        "test_files": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Titles of issues this depends on"
                        }
                    },
                    "required": ["title", "description", "target_files", "test_files", "depends_on"],
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
}


# Schema for architecture planning step
ArchitecturePlanSchema = {
    "name": "architecture_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "package_name": {
                "type": "string",
                "description": "Python package name (snake_case, no hyphens)"
            },
            "root_namespace": {
                "type": "string",
                "description": "Root namespace matching the package name"
            },
            "namespaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "namespace_path": {
                            "type": "string",
                            "description": "Directory path relative to workspace root, e.g. 'expense_tracker/models'"
                        },
                        "purpose": {"type": "string"}
                    },
                    "required": ["namespace_path", "purpose"],
                    "additionalProperties": False
                }
            },
            "domain_models": {
                "type": "array",
                "description": "Shared types and data classes used across modules",
                "items": {
                    "type": "object",
                    "properties": {
                        "class_name": {"type": "string"},
                        "filepath": {
                            "type": "string",
                            "description": "File path relative to workspace root"
                        },
                        "namespace_path": {"type": "string"},
                        "fields": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "type_hint": {"type": "string"},
                                    "description": {"type": "string"}
                                },
                                "required": ["name", "type_hint", "description"],
                                "additionalProperties": False
                            }
                        },
                        "methods": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "signature": {
                                        "type": "string",
                                        "description": "Full method signature, e.g. 'def total(self) -> float'"
                                    },
                                    "description": {"type": "string"}
                                },
                                "required": ["name", "signature", "description"],
                                "additionalProperties": False
                            }
                        },
                        "docstring": {"type": "string"}
                    },
                    "required": ["class_name", "filepath", "namespace_path", "fields", "methods", "docstring"],
                    "additionalProperties": False
                }
            },
            "modules": {
                "type": "array",
                "description": "Implementation modules with class/function signatures",
                "items": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string"},
                        "class_name": {
                            "type": "string",
                            "description": "Class name if this module defines a class, empty string for module-level functions"
                        },
                        "namespace_path": {"type": "string"},
                        "methods": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "signature": {"type": "string"},
                                    "description": {"type": "string"}
                                },
                                "required": ["name", "signature", "description"],
                                "additionalProperties": False
                            }
                        },
                        "docstring": {"type": "string"}
                    },
                    "required": ["filepath", "class_name", "namespace_path", "methods", "docstring"],
                    "additionalProperties": False
                }
            },
            "dependencies": {
                "type": "array",
                "description": "Import edges between modules",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_filepath": {"type": "string"},
                        "target_filepath": {"type": "string"},
                        "import_symbols": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    },
                    "required": ["source_filepath", "target_filepath", "import_symbols"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["package_name", "root_namespace", "namespaces", "domain_models", "modules", "dependencies"],
        "additionalProperties": False
    }
}


# Schema for architecture governance (drift review)
ArchitectureGovernanceSchema = {
    "name": "governance_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["approve", "reject", "modify"],
                "description": "Whether to approve, reject, or modify the unplanned changes"
            },
            "reason": {
                "type": "string",
                "description": "Explanation of the decision"
            },
            "plan_updates": {
                "type": "string",
                "description": "JSON string of partial architecture plan updates if decision is 'modify'. Empty string otherwise."
            }
        },
        "required": ["decision", "reason", "plan_updates"],
        "additionalProperties": False
    }
}

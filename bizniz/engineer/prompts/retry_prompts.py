"""Prompts for engineer retry strategies when issues fail."""

REPROMPT_TEMPLATE = """\
The following coding issue FAILED to be resolved by the orchestrator.

ORIGINAL ISSUE:
Title: {title}
Description:
{description}

FAILURE CONTEXT:
{failure_context}

STRATEGY USED: {strategy_used}

Rewrite this issue to be more specific and avoid the problems encountered.
Consider:
- Breaking ambiguous requirements into concrete, testable statements
- Specifying exact function signatures, parameter types, and return types
- Clarifying edge cases explicitly
- Removing any requirements that are contradictory or impossible
- Simplifying overly complex requirements

Respond with the rewritten issue description only. Make it clear and actionable.
"""

DECOMPOSE_TEMPLATE = """\
The following coding issue was too complex to solve in a single pass.

ORIGINAL ISSUE:
Title: {title}
Description:
{description}

FAILURE CONTEXT:
{failure_context}

Break this issue into smaller, independent sub-issues that can be solved
sequentially. Each sub-issue should:
- Be self-contained and testable on its own
- Build on the files created by previous sub-issues
- Have a clear, focused scope (one class, one module, or one feature)

Return the sub-issues as a JSON array.
"""

SCOPE_REDUCTION_TEMPLATE = """\
The following coding issue failed to be resolved.

ORIGINAL ISSUE:
Title: {title}
Description:
{description}

FAILURE CONTEXT:
{failure_context}

CURRENT WORKSPACE FILES:
{workspace_files}

Simplify this issue to only include what's strictly necessary. Remove any
non-essential requirements, optional features, or complex edge cases.
Focus on the core functionality that would constitute a minimal working version.

Respond with the simplified issue description only.
"""

DecomposeSchema = {
    "name": "decomposed_issues",
    "strict": True,
    "schema": {
        "type": "object",
        "required": ["sub_issues"],
        "properties": {
            "sub_issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title", "description", "target_files", "test_files"],
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "target_files": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["filepath", "action"],
                                "properties": {
                                    "filepath": {"type": "string"},
                                    "action": {
                                        "type": "string",
                                        "enum": ["create", "modify"],
                                    },
                                },
                                "additionalProperties": False,
                            },
                        },
                        "test_files": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    },
}

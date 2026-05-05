"""Tests for IssueEnrichmentAgent.

The AI call is mocked. We exercise the plumbing: prompt assembly,
JSON parsing tolerance, soft-fail behavior, and the to_coder_prompt_section
rendering.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bizniz.agents.issue_enrichment import (
    EnrichedIssue,
    IssueEnrichmentAgent,
    FieldSpec,
)


def _client_returning(text: str):
    client = MagicMock()
    client.get_text.return_value = (text, "job-1", [])
    return client


@pytest.fixture
def base_issue():
    return {
        "title": "Define Property Schemas",
        "description": "Create Pydantic schemas for Property creation and reading.",
        "target_files": [{"filepath": "app/schemas/properties.py", "action": "modify"}],
        "test_files": ["tests/unit/test_property_schemas.py"],
        "depends_on": [],
    }


def test_clean_response_parses_to_enriched_issue(base_issue):
    response = (
        '{"original_issue_title": "Define Property Schemas",'
        ' "original_issue_description": "Create Pydantic schemas...",'
        ' "required_fields": ['
        '   {"name": "address", "type": "str", "required": true, '
        '    "description": "Property street address", '
        '    "constraints": "min_length=1, max_length=255"},'
        '   {"name": "unit_count", "type": "int", "required": true, '
        '    "description": "Number of rentable units", '
        '    "constraints": "ge=1"}'
        ' ],'
        ' "optional_fields": ['
        '   {"name": "description", "type": "Optional[str]", "required": false, '
        '    "description": "Free-form description"}'
        ' ],'
        ' "confidence": "high"}'
    )
    agent = IssueEnrichmentAgent(client=_client_returning(response))
    enriched = agent.enrich(
        issue=base_issue,
        problem_statement=(
            "Property management for landlords. Create properties with "
            "addresses, unit counts, and descriptions."
        ),
    )

    assert enriched.confidence == "high"
    assert len(enriched.required_fields) == 2
    names = {f.name for f in enriched.required_fields}
    assert names == {"address", "unit_count"}
    assert enriched.optional_fields[0].name == "description"
    assert not enriched.is_empty()


def test_response_with_markdown_fence_parses_cleanly(base_issue):
    response = (
        "```json\n"
        '{"original_issue_title": "x", "original_issue_description": "y", '
        '"required_fields": [{"name": "a", "type": "str", "required": true, '
        '"description": "d"}], "confidence": "medium"}\n'
        "```"
    )
    agent = IssueEnrichmentAgent(client=_client_returning(response))
    enriched = agent.enrich(issue=base_issue, problem_statement="any")
    assert enriched.required_fields[0].name == "a"


def test_response_with_surrounding_prose_parses_cleanly(base_issue):
    response = (
        "Here is the enrichment:\n"
        '{"original_issue_title": "x", "original_issue_description": "y", '
        '"confidence": "medium"}\n'
        "Hope this helps."
    )
    agent = IssueEnrichmentAgent(client=_client_returning(response))
    enriched = agent.enrich(issue=base_issue, problem_statement="any")
    assert enriched.is_empty()
    assert enriched.confidence == "medium"


def test_unparseable_response_soft_fails(base_issue):
    agent = IssueEnrichmentAgent(client=_client_returning("not json at all"))
    enriched = agent.enrich(issue=base_issue, problem_statement="any")
    assert enriched.confidence == "low"
    assert any("unparseable" in n.lower() for n in enriched.notes)
    # Original issue traceability preserved
    assert enriched.original_issue_title == "Define Property Schemas"


def test_ai_exception_soft_fails(base_issue):
    client = MagicMock()
    client.get_text.side_effect = RuntimeError("network exploded")
    agent = IssueEnrichmentAgent(client=client)
    enriched = agent.enrich(issue=base_issue, problem_statement="any")
    assert enriched.confidence == "low"
    assert any("Enrichment unavailable" in n for n in enriched.notes)


def test_invalid_schema_response_soft_fails(base_issue):
    """AI emits JSON but with wrong types — should soft-fail rather
    than raise into the engineering flow."""
    response = '{"required_fields": "not a list"}'
    agent = IssueEnrichmentAgent(client=_client_returning(response))
    enriched = agent.enrich(issue=base_issue, problem_statement="any")
    assert enriched.confidence == "low"
    assert any("Schema validation failed" in n for n in enriched.notes)


def test_to_coder_prompt_section_renders_populated_fields():
    enriched = EnrichedIssue(
        original_issue_title="x",
        original_issue_description="y",
        required_fields=[
            FieldSpec(
                name="address", type="str", required=True,
                description="property street address",
                constraints="min_length=1",
            ),
        ],
        auth_requirements=["require_roles('landlord')"],
        confidence="high",
    )
    section = enriched.to_coder_prompt_section()
    assert "Required fields" in section
    assert "**address**" in section
    assert "min_length=1" in section
    assert "Auth requirements" in section
    assert "require_roles('landlord')" in section
    assert "high" in section


def test_to_coder_prompt_section_is_empty_for_empty_enrichment():
    enriched = EnrichedIssue(
        original_issue_title="x",
        original_issue_description="y",
    )
    assert enriched.to_coder_prompt_section() == ""
    assert enriched.is_empty()


def test_string_for_list_field_is_coerced_to_single_item_list(base_issue):
    """The AI sometimes returns a string instead of a list for fields
    like notes or auth_requirements. We coerce single strings to a
    one-element list rather than soft-failing on validation."""
    response = (
        '{"original_issue_title": "x", "original_issue_description": "y",'
        ' "auth_requirements": "Depends(get_current_user)",'
        ' "notes": "Use the existing User model from app/models/user.py"}'
    )
    agent = IssueEnrichmentAgent(client=_client_returning(response))
    enriched = agent.enrich(issue=base_issue, problem_statement="Build app")

    assert enriched.auth_requirements == ["Depends(get_current_user)"]
    assert enriched.notes == ["Use the existing User model from app/models/user.py"]
    assert enriched.confidence == "medium"  # default — not soft-failed


def test_error_case_with_condition_alias_is_renamed_to_when(base_issue):
    """The AI sometimes uses `condition` instead of the schema's `when`
    in error_cases. We rename rather than soft-fail."""
    response = (
        '{"original_issue_title": "x", "original_issue_description": "y",'
        ' "error_cases": ['
        '   {"status_code": 401, "condition": "Missing or invalid Authorization header.",'
        '    "detail": "Unauthorized"},'
        '   {"status": 403, "trigger": "Caller is not the owner.", "detail": "Forbidden"}'
        ' ]}'
    )
    agent = IssueEnrichmentAgent(client=_client_returning(response))
    enriched = agent.enrich(issue=base_issue, problem_statement="Build app")

    assert len(enriched.error_cases) == 2
    assert enriched.error_cases[0].when == "Missing or invalid Authorization header."
    assert enriched.error_cases[0].status_code == 401
    assert enriched.error_cases[1].when == "Caller is not the owner."
    assert enriched.error_cases[1].status_code == 403


def test_dependencies_alias_is_renamed_to_full_field_name(base_issue):
    """The AI sometimes returns `dependencies` instead of the full
    `dependencies_on_other_issues`. We rename rather than soft-fail."""
    response = (
        '{"original_issue_title": "x", "original_issue_description": "y",'
        ' "dependencies": ["Define Property Schemas", "Create Auth Module"]}'
    )
    agent = IssueEnrichmentAgent(client=_client_returning(response))
    enriched = agent.enrich(issue=base_issue, problem_statement="Build app")

    assert enriched.dependencies_on_other_issues == [
        "Define Property Schemas", "Create Auth Module",
    ]


def test_user_prompt_includes_problem_statement_and_target_files(base_issue):
    """Validate prompt assembly: target file paths and the problem
    statement must reach the AI."""
    captured = {}

    def fake_get_text(messages, response_format=None):
        captured["messages"] = messages
        return ('{"original_issue_title": "x", "original_issue_description": "y"}', "j", [])

    client = MagicMock()
    client.get_text.side_effect = fake_get_text

    agent = IssueEnrichmentAgent(client=client)
    agent.enrich(
        issue=base_issue,
        problem_statement="problems with addresses and unit counts",
        workspace_files=["app/main.py", "app/core/auth.py"],
        backend_openapi={
            "components": {"schemas": {
                "PropertyCreate": {
                    "properties": {"address": {}, "unit_count": {}},
                    "required": ["address"],
                },
            }},
        },
    )

    user_msg = captured["messages"][-1].content
    assert "Define Property Schemas" in user_msg
    assert "app/schemas/properties.py" in user_msg
    assert "addresses and unit counts" in user_msg
    assert "app/main.py" in user_msg
    assert "PropertyCreate" in user_msg
    assert "address*" in user_msg  # required marker on the OpenAPI schema render

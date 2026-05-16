"""Tests for the Decomposer agent (roadmap item 4 — first commit).

Covers:
  - Pydantic types (UnitOfWork, DecompositionResult) validate correctly
  - Prompt builder includes the right context
  - Decomposer.decompose: happy path, duplicate-id detection,
    empty-result rejection, schema-failure handling.

The LLM call itself is mocked — Decomposer's contract with the
client (request shape, response shape) is what we pin here. The
actual decomposition quality gets validated live during roadmap
item 9 (Claude perf test) with real builds.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.decomposer import Decomposer, DecomposerError
from bizniz.decomposer.prompts.decompose_prompt import (
    DECOMPOSE_SCHEMA,
    build_decompose_prompt,
)
from bizniz.decomposer.types import (
    DecompositionResult,
    UnitOfWork,
)
from bizniz.engineer.types import Issue


def _issue(**over) -> Issue:
    defaults = dict(
        id="BE-005",
        title="Implement companies API route",
        description=(
            "Add GET /api/companies + POST /api/companies. List "
            "returns paginated CompanyOut[]. Create accepts CompanyIn "
            "and returns CompanyOut on 201."
        ),
        target_files=["app/api/routes/companies.py"],
        success_criteria=["GET returns 200", "POST returns 201"],
    )
    defaults.update(over)
    return Issue(**defaults)


def _service() -> ServiceDefinition:
    return ServiceDefinition(
        name="backend",
        service_type="backend",
        framework="fastapi",
        language="python",
        description="API",
        workspace_name="backend",
        port=8000,
        depends_on=["db"],
        requirements=[],
        skeleton="fastapi",
    )


def _arch() -> SystemArchitecture:
    svc = _service()
    db = ServiceDefinition(
        name="db", service_type="database", framework="postgres",
        language="", description="db", workspace_name="db",
        port=5432, depends_on=[], requirements=[], skeleton="postgres",
    )
    return SystemArchitecture(
        project_name="crm_v1",
        project_slug="crm_v1",
        services=[svc, db],
        description="crm",
    )


def _unit(**over) -> dict:
    """Returns a dict suitable for the LLM's mocked response."""
    defaults = dict(
        id="BE-005-u1",
        summary="Create CompanyOut Pydantic schema",
        description=(
            "Add CompanyOut(BaseModel) with id, name, industry "
            "fields to app/api/schemas/companies.py."
        ),
        target_file="app/api/schemas/companies.py",
        kind="new_symbol",
        depends_on=[],
        expected_test_kind="unit_test",
        notes=None,
    )
    defaults.update(over)
    return defaults


# ── Type validation ──────────────────────────────────────────────


class TestUnitOfWorkValidation:
    def test_required_fields(self):
        u = UnitOfWork(
            id="u1", summary="x", description="y",
            target_file="f.py",
        )
        assert u.kind == "new_symbol"  # default
        assert u.expected_test_kind == "unit_test"  # default
        assert u.depends_on == []

    def test_kind_enum(self):
        UnitOfWork(
            id="u1", summary="x", description="y",
            target_file="f.py", kind="bundled_boilerplate",
        )
        with pytest.raises(Exception):
            UnitOfWork(
                id="u1", summary="x", description="y",
                target_file="f.py", kind="not_a_kind",
            )

    def test_test_kind_enum(self):
        with pytest.raises(Exception):
            UnitOfWork(
                id="u1", summary="x", description="y",
                target_file="f.py",
                expected_test_kind="invalid",
            )


class TestDecompositionResultValidation:
    def test_confidence_range(self):
        with pytest.raises(Exception):
            DecompositionResult(
                issue_id="i1", ordered_units=[], confidence=1.5,
            )
        with pytest.raises(Exception):
            DecompositionResult(
                issue_id="i1", ordered_units=[], confidence=-0.1,
            )

    def test_default_confidence_is_one(self):
        r = DecompositionResult(issue_id="i1")
        assert r.confidence == 1.0


# ── Prompt builder ───────────────────────────────────────────────


class TestPromptBuilder:
    def test_includes_issue_basics(self):
        out = build_decompose_prompt(
            issue_id="BE-005",
            issue_title="Companies route",
            issue_description="Add the thing.",
            issue_target_files=["app/api/routes/companies.py"],
            issue_success_criteria=["200 OK"],
            service_name="backend",
            service_framework="fastapi",
            architecture_summary="2 services",
        )
        assert "BE-005" in out
        assert "Companies route" in out
        assert "Add the thing." in out
        assert "app/api/routes/companies.py" in out
        assert "fastapi" in out

    def test_existing_files_hint_included(self):
        out = build_decompose_prompt(
            issue_id="BE-005", issue_title="x",
            issue_description="y", issue_target_files=[],
            issue_success_criteria=[],
            service_name="backend", service_framework="fastapi",
            architecture_summary="",
            existing_files_hint="app/models/user.py\napp/db/base.py",
        )
        assert "app/models/user.py" in out
        assert "Workspace state" in out

    def test_no_hint_omits_section(self):
        out = build_decompose_prompt(
            issue_id="BE-005", issue_title="x",
            issue_description="y", issue_target_files=[],
            issue_success_criteria=[],
            service_name="backend", service_framework="fastapi",
            architecture_summary="",
        )
        assert "Workspace state" not in out


class TestSchema:
    def test_schema_top_level_shape(self):
        # Pin the schema shape — downstream prompt-rendering relies on
        # ``required`` + the enum'd ``kind`` field.
        assert DECOMPOSE_SCHEMA["name"] == "DecompositionResult"
        props = DECOMPOSE_SCHEMA["schema"]["properties"]
        assert "ordered_units" in props
        assert "confidence" in props
        unit_schema = props["ordered_units"]["items"]
        assert "target_file" in unit_schema["properties"]
        assert "depends_on" in unit_schema["properties"]
        # kind enum has exactly the three values from types.py.
        kinds = set(unit_schema["properties"]["kind"]["enum"])
        assert kinds == {
            "new_symbol", "new_behavior", "bundled_boilerplate",
        }


# ── Agent dispatch ───────────────────────────────────────────────


class _FakeClient:
    """Stand-in for BaseAIClient. Returns ``response`` on every call."""

    def __init__(self, response: dict):
        self._response = response
        self.calls = 0
        self.last_messages = None

    def get_text(self, messages=None, **kwargs):
        self.calls += 1
        self.last_messages = messages
        import json as _json
        return _json.dumps(self._response), "fake-job-id", []


class TestDecomposerDispatch:
    def _make_agent(self, response: dict) -> tuple:
        client = _FakeClient(response)
        agent = Decomposer(client=client)  # type: ignore[arg-type]
        return agent, client

    def test_happy_path(self):
        response = {
            "issue_id": "BE-005",
            "ordered_units": [
                _unit(id="BE-005-u1"),
                _unit(
                    id="BE-005-u2",
                    summary="Create CompanyIn schema",
                    target_file="app/api/schemas/companies.py",
                    depends_on=["BE-005-u1"],
                ),
                _unit(
                    id="BE-005-u3",
                    summary="Wire GET /api/companies",
                    target_file="app/api/routes/companies.py",
                    depends_on=["BE-005-u1"],
                ),
            ],
            "confidence": 0.87,
        }
        agent, client = self._make_agent(response)
        result = agent.decompose(_issue(), _service(), _arch())
        assert result.issue_id == "BE-005"
        assert len(result.ordered_units) == 3
        assert result.confidence == 0.87
        # All units have valid kind defaults applied.
        assert all(
            u.kind in ("new_symbol", "new_behavior", "bundled_boilerplate")
            for u in result.ordered_units
        )
        assert client.calls == 1

    def test_forces_issue_id_to_match(self):
        # If model echoes a different issue_id, we pin it back.
        response = {
            "issue_id": "WRONG-ID",  # model hallucinated
            "ordered_units": [_unit()],
            "confidence": 1.0,
        }
        agent, _ = self._make_agent(response)
        result = agent.decompose(_issue(id="BE-005"), _service(), _arch())
        assert result.issue_id == "BE-005"

    def test_empty_units_raises(self):
        response = {
            "issue_id": "BE-005",
            "ordered_units": [],
            "confidence": 0.5,
        }
        agent, _ = self._make_agent(response)
        with pytest.raises(DecomposerError, match="zero units"):
            agent.decompose(_issue(), _service(), _arch())

    def test_duplicate_unit_ids_raises(self):
        response = {
            "issue_id": "BE-005",
            "ordered_units": [
                _unit(id="dup"),
                _unit(id="dup", summary="other"),
            ],
            "confidence": 0.9,
        }
        agent, _ = self._make_agent(response)
        with pytest.raises(DecomposerError, match="duplicate unit ids"):
            agent.decompose(_issue(), _service(), _arch())

    def test_schema_validation_failure_raises(self):
        # Missing required ``target_file`` in a unit.
        response = {
            "issue_id": "BE-005",
            "ordered_units": [{
                "id": "u1", "summary": "x", "description": "y",
                # target_file deliberately omitted
                "kind": "new_symbol", "depends_on": [],
                "expected_test_kind": "unit_test",
            }],
            "confidence": 0.9,
        }
        agent, _ = self._make_agent(response)
        with pytest.raises(DecomposerError, match="schema"):
            agent.decompose(_issue(), _service(), _arch())

    def test_existing_files_hint_threaded_to_prompt(self):
        response = {
            "issue_id": "BE-005",
            "ordered_units": [_unit()],
            "confidence": 1.0,
        }
        agent, client = self._make_agent(response)
        agent.decompose(
            _issue(), _service(), _arch(),
            existing_files_hint="app/models/user.py exists",
        )
        user_msg = next(
            m for m in client.last_messages if m.role == "user"
        )
        assert "app/models/user.py exists" in user_msg.content

"""Unit tests for ``CoderTesterAgent``."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.coder.types import Issue
from bizniz.coder_tester.agent import (
    CoderTesterAgent,
    CoderTesterError,
)
from bizniz.coder_tester.types import FilledFile
from bizniz.quality_engineer.types import CapabilitySpec


def _service() -> ServiceDefinition:
    return ServiceDefinition(
        name="backend",
        service_type="backend",
        framework="fastapi",
        language="python",
        workspace_name="backend",
        port=8000,
        description="API backend",
        depends_on=[],
    )


def _issue(
    issue_id: str = "BE-001",
    target_files: list = None,
    test_files: list = None,
) -> Issue:
    return Issue(
        id=issue_id,
        title="Add /me endpoint",
        description="Return the current user's profile.",
        service="backend",
        language="python",
        target_files=target_files or ["app/api/routes/me.py"],
        test_files=test_files or ["tests/test_me.py"],
        success_criteria=["GET /me returns 200 for authenticated user"],
        spec_refs=["me_endpoint"],
        depends_on=[],
    )


def _capability() -> CapabilitySpec:
    return CapabilitySpec(
        id="me_endpoint",
        name="Current user lookup",
        description="GET /me returns the authenticated user's profile.",
        test_scenarios=["happy path returns 200", "no auth returns 401"],
    )


def _ok_llm_output(issue_id: str = "BE-001") -> dict:
    """Mock LLM output — valid envelope."""
    return {
        "issue_id": issue_id,
        "filled_files": [
            {
                "path": "app/api/routes/me.py",
                "content": "# real implementation\nfrom fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/me')\ndef me(): return {}\n",
                "role": "code",
            },
            {
                "path": "tests/test_me.py",
                "content": "def test_me_happy(): assert True\n",
                "role": "test",
            },
        ],
        "notes": "",
    }


# ── Happy path ──────────────────────────────────────────────────────


class TestHappyPath:
    def test_returns_filled_envelope_with_code_and_test(self):
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=_ok_llm_output(),
        ):
            agent = CoderTesterAgent(client=MagicMock(spec=BaseAIClient))
            result = agent.code_issue(
                issue=_issue(),
                service=_service(),
                seeded_files=[],
                capabilities=[_capability()],
            )
        assert result.issue_id == "BE-001"
        assert len(result.filled_files) == 2
        paths = {f.path for f in result.filled_files}
        assert paths == {"app/api/routes/me.py", "tests/test_me.py"}
        roles = {f.role for f in result.filled_files}
        assert roles == {"code", "test"}

    def test_passes_label_to_call_with_retry(self):
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=_ok_llm_output(),
        ) as mock_call:
            agent = CoderTesterAgent(client=MagicMock(spec=BaseAIClient))
            agent.code_issue(
                issue=_issue("BE-XYZ"),
                service=_service(),
                seeded_files=[],
                capabilities=[],
            )
        kwargs = mock_call.call_args.kwargs
        assert kwargs["label"] == "CoderTesterAgent[BE-XYZ]"


# ── Path-contract enforcement ──────────────────────────────────────


class TestPathContract:
    def test_out_of_scope_path_raises(self):
        bad = {
            "issue_id": "BE-001",
            "filled_files": [
                {
                    "path": "app/api/routes/me.py",
                    "content": "x",
                    "role": "code",
                },
                {
                    # Path NOT in the issue's target_files or test_files.
                    "path": "app/secrets/leak.py",
                    "content": "x",
                    "role": "code",
                },
            ],
            "notes": "",
        }
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=bad,
        ):
            agent = CoderTesterAgent(client=MagicMock(spec=BaseAIClient))
            with pytest.raises(CoderTesterError, match="outside .* declared scope"):
                agent.code_issue(
                    issue=_issue(),
                    service=_service(),
                    seeded_files=[],
                    capabilities=[],
                )

    def test_missing_declared_path_is_warning_not_failure(self):
        """Agent fails to produce one of the declared paths — that's
        the per-issue validator's job to surface as a real failure.
        The agent itself logs a warning but returns what it has."""
        partial = {
            "issue_id": "BE-001",
            "filled_files": [
                {
                    "path": "app/api/routes/me.py",
                    "content": "x",
                    "role": "code",
                },
                # tests/test_me.py is missing — agent shipped without it.
            ],
            "notes": "",
        }
        statuses: list = []
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=partial,
        ):
            agent = CoderTesterAgent(
                client=MagicMock(spec=BaseAIClient),
                on_status=statuses.append,
            )
            result = agent.code_issue(
                issue=_issue(),
                service=_service(),
                seeded_files=[],
                capabilities=[],
            )
        assert len(result.filled_files) == 1
        assert any(
            "declared paths not produced" in s for s in statuses
        )

    def test_empty_filled_files_raises(self):
        empty = {"issue_id": "BE-001", "filled_files": [], "notes": ""}
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=empty,
        ):
            agent = CoderTesterAgent(client=MagicMock(spec=BaseAIClient))
            with pytest.raises(CoderTesterError, match="empty filled_files"):
                agent.code_issue(
                    issue=_issue(),
                    service=_service(),
                    seeded_files=[],
                    capabilities=[],
                )


# ── Issue id echo ──────────────────────────────────────────────────


class TestIssueIdEcho:
    def test_echo_mismatch_is_warning_not_failure(self):
        wrong_echo = _ok_llm_output(issue_id="WRONG-ID")
        statuses: list = []
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=wrong_echo,
        ):
            agent = CoderTesterAgent(
                client=MagicMock(spec=BaseAIClient),
                on_status=statuses.append,
            )
            result = agent.code_issue(
                issue=_issue("BE-001"),
                service=_service(),
                seeded_files=[],
                capabilities=[],
            )
        # Result still carries the agent's echoed id; warning logged.
        assert result.issue_id == "BE-001"
        assert any("echoed issue_id=" in s for s in statuses)


# ── Notes plumbing ──────────────────────────────────────────────────


class TestNotes:
    def test_notes_surfaced_in_log_and_result(self):
        out = _ok_llm_output()
        out["notes"] = "Skipped foo handler — depends on BE-002."
        statuses: list = []
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=out,
        ):
            agent = CoderTesterAgent(
                client=MagicMock(spec=BaseAIClient),
                on_status=statuses.append,
            )
            result = agent.code_issue(
                issue=_issue(),
                service=_service(),
                seeded_files=[],
                capabilities=[],
            )
        assert result.notes == "Skipped foo handler — depends on BE-002."
        assert any("Skipped foo handler" in s for s in statuses)


# ── Schema salvage (Haiku occasionally drops role) ────────────────


class TestSchemaSalvage:
    """Live debrief 2026-05-19 recipe_v4_v6: Haiku produced
    ``role: null`` or omitted ``role`` on backend issues, crashing 8/9
    issues in the run. Agent now infers role from path extension as a
    salvage step before Pydantic validation."""

    def test_null_role_inferred_from_test_path(self):
        # Paths must match the issue's declared target_files/test_files
        # so the path-contract gate doesn't reject before salvage matters.
        out = {
            "issue_id": "BE-001",
            "filled_files": [
                {"path": "tests/test_me.py", "content": "x", "role": None},
                {"path": "app/api/routes/me.py", "content": "y", "role": None},
            ],
            "notes": "",
        }
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=out,
        ):
            agent = CoderTesterAgent(client=MagicMock(spec=BaseAIClient))
            result = agent.code_issue(
                issue=_issue(),
                service=_service(),
                seeded_files=[],
                capabilities=[],
            )
        by_path = {f.path: f.role for f in result.filled_files}
        assert by_path["tests/test_me.py"] == "test"
        assert by_path["app/api/routes/me.py"] == "code"

    def test_missing_role_field_inferred(self):
        out = {
            "issue_id": "BE-001",
            "filled_files": [
                {"path": "tests/test_me.py", "content": "x"},
                {"path": "app/api/routes/me.py", "content": "y"},
            ],
            "notes": "",
        }
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=out,
        ):
            agent = CoderTesterAgent(client=MagicMock(spec=BaseAIClient))
            result = agent.code_issue(
                issue=_issue(),
                service=_service(),
                seeded_files=[],
                capabilities=[],
            )
        roles = {f.role for f in result.filled_files}
        assert roles == {"code", "test"}

    def test_validation_error_includes_field_detail(self):
        # path missing entirely — salvage can't help. Error message
        # must surface the actual Pydantic field detail (used to be
        # truncated to "1 validation error for FilledFile").
        bad = {
            "issue_id": "BE-001",
            "filled_files": [{"content": "x", "role": "code"}],
            "notes": "",
        }
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=bad,
        ):
            agent = CoderTesterAgent(client=MagicMock(spec=BaseAIClient))
            with pytest.raises(CoderTesterError) as exc_info:
                agent.code_issue(
                    issue=_issue(),
                    service=_service(),
                    seeded_files=[],
                    capabilities=[],
                )
        msg = str(exc_info.value)
        # Either pydantic surfaces "path" in errors() or the item-keys
        # list shows path is missing — either way the user knows which
        # field broke.
        assert "path" in msg or "item keys" in msg


# ── Prompt building ────────────────────────────────────────────────


class TestPromptConstruction:
    def test_user_prompt_includes_issue_capability_and_seeded(self):
        seeded = [FilledFile(
            path="app/api/routes/me.py",
            content="from fastapi import APIRouter\nrouter = APIRouter()\n# TODO",
            role="code",
        )]
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=_ok_llm_output(),
        ) as mock_call:
            agent = CoderTesterAgent(client=MagicMock(spec=BaseAIClient))
            agent.code_issue(
                issue=_issue(),
                service=_service(),
                seeded_files=seeded,
                capabilities=[_capability()],
                sibling_issue_summaries=["BE-002 — User schema (app/models/user.py)"],
            )
        user_msg = mock_call.call_args.kwargs["messages"][1]
        body = user_msg.content
        assert "BE-001" in body
        assert "/me endpoint" in body
        assert "me_endpoint" in body                   # capability id
        assert "happy path returns 200" in body       # test scenarios surfaced
        assert "app/api/routes/me.py" in body
        assert "BE-002" in body                        # sibling summary

    def test_skeleton_md_and_auth_contract_included_when_supplied(self):
        with patch(
            "bizniz.coder_tester.agent.call_with_retry",
            return_value=_ok_llm_output(),
        ) as mock_call:
            agent = CoderTesterAgent(client=MagicMock(spec=BaseAIClient))
            agent.code_issue(
                issue=_issue(),
                service=_service(),
                seeded_files=[],
                capabilities=[],
                skeleton_md="## Skeleton X\n- foo",
                auth_contract="## Auth Y\n- bar",
            )
        body = mock_call.call_args.kwargs["messages"][1].content
        assert "Skeleton X" in body
        assert "Auth Y" in body

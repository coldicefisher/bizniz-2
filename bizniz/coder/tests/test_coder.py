"""End-to-end tests for the Coder agent. Mocks the LLM client and
canned action sequences."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.coder.agent import Coder
from bizniz.coder.types import CoderResult, Issue
from bizniz.quality_engineer.types import (
    CapabilitySpec,
    EnrichedSpec,
    Field as SpecField,
)
from bizniz.workspace.local_workspace import LocalWorkspace


# ── Fixtures ───────────────────────────────────────────────────────────


def _arch():
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="API",
                workspace_name="backend", port=8000, depends_on=["db"],
            ),
            ServiceDefinition(
                name="db", service_type="database", framework="postgres",
                language="sql", description="db", workspace_name="db",
                port=5432,
            ),
        ],
    )


def _spec():
    return EnrichedSpec(
        milestone_name="M1",
        capabilities=[
            CapabilitySpec(
                id="cap_x", name="Cap X", description="description",
                inputs=[SpecField(name="email", type="string", required=True,
                                  constraints=[], description="")],
                outputs=[], validation_rules=[], error_cases=[],
                edge_cases=[], auth_required=True, allowed_roles=["user"],
                test_scenarios=["happy"],
            ),
        ],
    )


def _issue(target=("app/users.py",), tests=("tests/test_users.py",)):
    return Issue(
        id="I1", title="users", description="d",
        service="backend", language="python",
        target_files=list(target),
        test_files=list(tests),
        success_criteria=["users created"],
        spec_refs=["cap_x"],
        depends_on=[],
    )


def _action(action_type, **kw):
    base = {
        "thinking": "x", "action": action_type,
        "path": "", "new_content": "", "query": "", "service": "",
        "url": "", "request_data": "", "command": "", "sql": "", "token": "",
        "summary": "", "status": "passed", "notes": [],
    }
    base.update(kw)
    return json.dumps(base)


def _client_with(actions):
    c = MagicMock(spec=BaseAIClient)
    c.try_create_cache = MagicMock(return_value=None)
    c.get_text.side_effect = [(a, "j", []) for a in actions]
    return c


def _coder(client, tmp_path):
    return Coder(
        client=client,
        workspace=LocalWorkspace(root=tmp_path),
        compose_path="/p/proj/compose.yml",
        target_service="backend",
        tool_iterations=10,
        timeout_seconds=10,
    )


# ── Tool surface ───────────────────────────────────────────────────────


class TestToolSurface:
    def test_includes_validate_symbols(self, tmp_path):
        coder = _coder(MagicMock(spec=BaseAIClient), tmp_path)
        coder._issue = _issue()
        coder._handlers = coder._build_handlers("backend")
        assert "validate_symbols" in coder.tool_handlers()
        assert "write_file" in coder.tool_handlers()
        assert "run_tests" in coder.tool_handlers()
        assert "smoke_import" in coder.tool_handlers()


# ── validate_symbols handler ───────────────────────────────────────────


class TestValidateSymbolsHandler:
    def test_no_target_files_yet(self, tmp_path):
        coder = _coder(MagicMock(spec=BaseAIClient), tmp_path)
        coder._issue = _issue()
        coder._target_files_written = []
        out = coder._handle_validate_symbols({})
        assert "no target_files" in out

    def test_non_python_skipped(self, tmp_path):
        coder = _coder(MagicMock(spec=BaseAIClient), tmp_path)
        coder._issue = Issue(
            id="I1", title="t", description="d",
            service="frontend", language="typescript",
            target_files=["src/x.tsx"], test_files=[], success_criteria=[],
            spec_refs=[], depends_on=[],
        )
        coder._target_files_written = ["src/x.tsx"]
        out = coder._handle_validate_symbols({})
        assert "skipped" in out
        assert "not yet supported" in out

    def test_passes_on_clean_python(self, tmp_path):
        # Need a requirements.txt so third-party detection works.
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "users.py").write_text(
            "import os\nfrom fastapi import APIRouter\n"
        )
        coder = _coder(MagicMock(spec=BaseAIClient), tmp_path)
        coder._issue = _issue()
        coder._target_files_written = ["app/users.py"]
        out = coder._handle_validate_symbols({})
        assert "PASSED" in out

    def test_flags_hallucinated_import(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "users.py").write_text(
            "from totally_made_up_lib import Stuff\n"
        )
        coder = _coder(MagicMock(spec=BaseAIClient), tmp_path)
        coder._issue = _issue()
        coder._target_files_written = ["app/users.py"]
        out = coder._handle_validate_symbols({})
        assert "FAILED" in out
        assert "totally_made_up_lib" in out


# ── End-to-end tool loop ───────────────────────────────────────────────


class TestEndToEnd:
    def test_minimal_path(self, tmp_path):
        # Coder writes file, validates, writes test, runs tests, submits.
        # Stub run_tests to return the deterministic green verdict so the
        # submit_code gate accepts status='passed'.
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        actions = [
            _action("write_file", path="app/users.py",
                    new_content="from fastapi import APIRouter\nrouter = APIRouter()\n"),
            _action("validate_symbols"),
            _action("write_file", path="tests/test_users.py",
                    new_content="def test_x(): assert True\n"),
            _action("run_tests", path="tests/"),
            _action("submit_code", status="passed", summary="ok"),
        ]
        coder = _coder(_client_with(actions), tmp_path)
        # Stub the run_tests handler to return the verdict prefix.
        coder._handlers = coder._build_handlers("backend")
        coder._handlers["run_tests"] = lambda a: "TESTS PASSED\n(exit code: 0)\n"
        # Re-attach the wrapper that captures _last_test_output.
        original_run_tests = coder._handlers["run_tests"]

        def run_tests_tracked(action):
            result = original_run_tests(action)
            coder._last_test_output = result
            return result
        coder._handlers["run_tests"] = run_tests_tracked

        # Skip code_issue's _build_handlers reset by invoking .run() directly
        # with the prepared handlers. We still need to mark _issue and clear
        # state like code_issue does.
        from bizniz.coder.prompts.initial_context import build_coder_initial_context
        coder._issue = _issue()
        coder._target_files_written = []
        coder._test_files_written = []
        coder._last_test_output = ""
        initial = build_coder_initial_context(
            issue=_issue(), architecture=_arch(), enriched_spec=_spec(),
        )
        result = coder.run(initial)
        assert isinstance(result, CoderResult)
        assert result.status == "passed"
        assert result.target_files_written == ["app/users.py"]
        assert result.test_files_written == ["tests/test_users.py"]

    def test_passed_without_run_tests_is_rejected(self, tmp_path):
        # LLM tries to submit passed without running tests → rejected.
        # After rejection, LLM submits partial → accepted.
        actions = [
            _action("submit_code", status="passed", summary="lying"),
            _action("submit_code", status="partial", summary="ok honest"),
        ]
        coder = _coder(_client_with(actions), tmp_path)
        result = coder.code_issue(
            issue=_issue(), architecture=_arch(), enriched_spec=_spec(),
        )
        assert result.status == "partial"

    def test_initial_context_has_issue_only(self, tmp_path):
        # Use status='partial' to bypass the green-tests gate; this test
        # only cares about the prompt assembly.
        actions = [_action("submit_code", status="partial", summary="x")]
        coder = _coder(_client_with(actions), tmp_path)
        coder.code_issue(
            issue=_issue(target=("a.py",), tests=("t.py",)),
            architecture=_arch(),
            enriched_spec=_spec(),
        )
        sent = coder._client.get_text.call_args.kwargs["messages"]
        user_msg = next(m["content"] for m in sent if m["role"] == "user")
        # Issue id + target file mentioned
        assert "I1" in user_msg
        assert "a.py" in user_msg
        assert "t.py" in user_msg
        # Capability spec_refs filtered to the issue's
        assert "cap_x" in user_msg
        # Service info included for the issue's service
        assert "backend" in user_msg.lower()


# ── Schema correctness ─────────────────────────────────────────────────


class TestWorkspacePrefixStrip:
    """ServicePlanner emits paths like 'backend/app/users.py' thinking
    workspace is project-root. Coder must strip the prefix or files
    land double-nested at backend/backend/app/users.py and the
    skeleton can't import them."""

    def test_strip_known_prefix(self, tmp_path):
        coder = Coder(
            client=MagicMock(spec=BaseAIClient),
            workspace=LocalWorkspace(root=tmp_path),
            compose_path="/p/c.yml", target_service="backend",
            workspace_name="backend",
        )
        assert coder._strip_workspace_prefix("backend/app/users.py") \
            == "app/users.py"

    def test_unprefixed_path_unchanged(self, tmp_path):
        coder = Coder(
            client=MagicMock(spec=BaseAIClient),
            workspace=LocalWorkspace(root=tmp_path),
            compose_path="/p/c.yml", target_service="backend",
            workspace_name="backend",
        )
        assert coder._strip_workspace_prefix("app/users.py") == "app/users.py"

    def test_idempotent(self, tmp_path):
        coder = Coder(
            client=MagicMock(spec=BaseAIClient),
            workspace=LocalWorkspace(root=tmp_path),
            compose_path="/p/c.yml", target_service="backend",
            workspace_name="backend",
        )
        once = coder._strip_workspace_prefix("backend/app/users.py")
        twice = coder._strip_workspace_prefix(once)
        assert twice == "app/users.py"

    def test_falls_back_to_target_service_when_workspace_name_unset(
        self, tmp_path,
    ):
        coder = Coder(
            client=MagicMock(spec=BaseAIClient),
            workspace=LocalWorkspace(root=tmp_path),
            compose_path="/p/c.yml", target_service="backend",
            # workspace_name not provided
        )
        assert coder._strip_workspace_prefix("backend/x.py") == "x.py"


class TestSchema:
    def test_validate_symbols_in_action_enum(self):
        from bizniz.coder.prompts.schema import CODER_ACTION_SCHEMA
        actions = CODER_ACTION_SCHEMA["schema"]["properties"]["action"]["enum"]
        assert "validate_symbols" in actions
        assert "submit_code" in actions
        assert "write_file" in actions
        # No plan-related actions (not Engineer)
        assert "submit_plan" not in actions
        assert "revise_plan" not in actions

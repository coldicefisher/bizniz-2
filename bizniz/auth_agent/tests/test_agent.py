"""Tests for the AuthAgent.

Mock the FA orchestrator + LLM client. We exercise:
  - Configure-mode tool surface includes ``fa_apply_spec``
  - Audit-mode tool surface excludes ``fa_apply_spec``
  - System prompt differs by mode
  - submit_contract terminal action returns AuthAgentResult
  - Initial context lists stack languages from the architecture
  - Contract markdown is written to disk when ``write_contract_to`` is set
"""
import json
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.auth_orchestrators.fusionauth_orchestrator import FusionAuthOrchestrator
from bizniz.auth_agent.agent import AuthAgent
from bizniz.auth_agent.types import AuthAgentResult
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.workspace.base_workspace import BaseWorkspace


# ── Fixtures ──────────────────────────────────────────────────────────────

def _arch():
    return SystemArchitecture(
        project_name="Mini CRM",
        project_slug="mini_crm",
        description="seed",
        services=[
            ServiceDefinition(
                name="database", service_type="database", framework="postgres",
                language="sql", description="Primary store.",
                workspace_name="postgres", port=5432,
            ),
            ServiceDefinition(
                name="auth", service_type="auth", framework="fusionauth",
                language="yaml", description="Identity provider.",
                workspace_name="fusionauth", port=9011, depends_on=["database"],
            ),
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="REST API.",
                workspace_name="backend", port=8000, depends_on=["auth", "database"],
            ),
            ServiceDefinition(
                name="frontend", service_type="frontend", framework="react",
                language="typescript", description="UI.",
                workspace_name="frontend", port=5173, depends_on=["backend"],
            ),
        ],
    )


def _terminal_response(contract="# Auth Contract\nstub", summary="done", applied=()):
    """Minimal terminal action — the agent submits this to end the loop."""
    return json.dumps({
        "thinking": "submitting",
        "action": "submit_contract",
        "primary_app_id": "",
        "tenant_id": "",
        "email": "",
        "password": "",
        "spec_json": "",
        "token": "",
        "contract_markdown": contract,
        "summary": summary,
        "applied_changes": list(applied),
    }), "job-id", []


@pytest.fixture
def mock_orch():
    return MagicMock(spec=FusionAuthOrchestrator)


@pytest.fixture
def mock_workspace():
    return MagicMock(spec=BaseWorkspace)


# ── Mode-specific tool surface ───────────────────────────────────────────

class TestToolSurface:
    def test_configure_mode_includes_fa_apply_spec(self, mock_orch, mock_workspace):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _terminal_response()
        agent = AuthAgent(
            client=client, workspace=mock_workspace, fa_orchestrator=mock_orch,
        )
        agent.configure(
            problem_slice="add roles", architecture=_arch(),
            primary_app_id="app-uuid", tenant_id="tenant-uuid",
        )
        # After running, _handlers reflects the mode that was last set up.
        assert "fa_apply_spec" in agent.tool_handlers()
        assert "fa_smoke_login" in agent.tool_handlers()
        assert "fa_diagnose" in agent.tool_handlers()
        assert "decode_jwt" in agent.tool_handlers()

    def test_audit_mode_excludes_fa_apply_spec(self, mock_orch, mock_workspace):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _terminal_response()
        agent = AuthAgent(
            client=client, workspace=mock_workspace, fa_orchestrator=mock_orch,
        )
        agent.audit(
            architecture=_arch(),
            primary_app_id="app-uuid", tenant_id="tenant-uuid",
        )
        assert "fa_apply_spec" not in agent.tool_handlers()
        # Read-only tools are still there
        assert "fa_smoke_login" in agent.tool_handlers()
        assert "fa_diagnose" in agent.tool_handlers()
        assert "decode_jwt" in agent.tool_handlers()


# ── Mode-specific prompt ─────────────────────────────────────────────────

class TestSystemPrompt:
    def test_configure_prompt_says_configure(self, mock_orch, mock_workspace):
        agent = AuthAgent(
            client=MagicMock(spec=BaseAIClient), workspace=mock_workspace,
            fa_orchestrator=mock_orch,
        )
        agent._mode = "configure"
        prompt = agent.system_prompt
        assert "MODE: configure" in prompt or "Mode: configure" in prompt
        assert "fa_apply_spec" in prompt

    def test_audit_prompt_says_audit_only(self, mock_orch, mock_workspace):
        agent = AuthAgent(
            client=MagicMock(spec=BaseAIClient), workspace=mock_workspace,
            fa_orchestrator=mock_orch,
        )
        agent._mode = "audit"
        prompt = agent.system_prompt
        assert "audit" in prompt.lower()
        assert "do not have access" in prompt.lower() or "never attempt to mutate" in prompt.lower()


# ── Terminal action ──────────────────────────────────────────────────────

class TestTerminalAction:
    def test_submit_contract_returns_result(self, mock_orch, mock_workspace):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _terminal_response(
            contract="# Real contract", summary="all good",
            applied=["created role landlord", "created user landlord@example.com"],
        )
        agent = AuthAgent(
            client=client, workspace=mock_workspace, fa_orchestrator=mock_orch,
        )
        result = agent.configure(
            problem_slice="set up auth",
            architecture=_arch(),
            primary_app_id="app-uuid",
            tenant_id="tenant-uuid",
        )
        assert isinstance(result, AuthAgentResult)
        assert result.mode == "configure"
        assert result.contract_markdown == "# Real contract"
        assert result.summary == "all good"
        assert "created role landlord" in result.applied_changes


# ── Initial context ──────────────────────────────────────────────────────

class TestInitialContext:
    def test_lists_stack_languages_from_architecture(self, mock_orch, mock_workspace):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _terminal_response()
        agent = AuthAgent(
            client=client, workspace=mock_workspace, fa_orchestrator=mock_orch,
        )
        agent.configure(
            problem_slice="x", architecture=_arch(),
            primary_app_id="app-uuid", tenant_id="tenant-uuid",
        )
        # The initial user message (second message in the conversation)
        # should mention the stack languages.
        sent = client.get_text.call_args.kwargs.get("messages")
        user_text = next(
            m["content"] for m in sent if m.get("role") == "user"
        )
        assert "python" in user_text.lower()
        assert "typescript" in user_text.lower()
        # And the FA coordinates
        assert "app-uuid" in user_text
        assert "tenant-uuid" in user_text

    def test_includes_existing_contract_when_provided(self, mock_orch, mock_workspace):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _terminal_response()
        agent = AuthAgent(
            client=client, workspace=mock_workspace, fa_orchestrator=mock_orch,
        )
        agent.audit(
            architecture=_arch(),
            primary_app_id="app", tenant_id="tenant",
            existing_contract="# Old contract\n- iss: foo.com",
        )
        sent = client.get_text.call_args.kwargs.get("messages")
        user_text = next(
            m["content"] for m in sent if m.get("role") == "user"
        )
        assert "Old contract" in user_text
        assert "foo.com" in user_text


# ── Contract write-out ───────────────────────────────────────────────────

class TestContractWriteout:
    def test_writes_contract_to_disk_when_path_provided(
        self, mock_orch, mock_workspace, tmp_path,
    ):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _terminal_response(
            contract="# Final contract\nbody", summary="ok",
        )
        agent = AuthAgent(
            client=client, workspace=mock_workspace, fa_orchestrator=mock_orch,
        )
        out = tmp_path / "AUTH_CONTRACT.md"
        result = agent.configure(
            problem_slice="x", architecture=_arch(),
            primary_app_id="app", tenant_id="tenant",
            write_contract_to=out,
        )
        assert result.contract_path == str(out)
        assert out.read_text() == "# Final contract\nbody"

    def test_no_write_when_path_not_provided(
        self, mock_orch, mock_workspace, tmp_path,
    ):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = _terminal_response(contract="# x")
        agent = AuthAgent(
            client=client, workspace=mock_workspace, fa_orchestrator=mock_orch,
        )
        result = agent.audit(
            architecture=_arch(),
            primary_app_id="app", tenant_id="tenant",
        )
        assert result.contract_path is None
        assert result.contract_markdown == "# x"

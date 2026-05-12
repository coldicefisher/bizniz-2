"""Tests for ClaudeCliDebugger.

Mirrors the test shape used for ClaudeCliCoder — subprocess is
mocked, we verify command shape, prompt assembly, and diagnosis
parsing.
"""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bizniz.agents.debugger.claude_cli_debugger import ClaudeCliDebugger
from bizniz.agents.debugger.types import (
    AgenticDebuggerError,
    AgenticDebuggerTimeoutError,
    AgenticDiagnosis,
)


def _fake_proc(result_text: str, returncode: int = 0):
    payload = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result_text,
        "session_id": "sid-dbg",
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 500, "output_tokens": 200},
    })
    p = MagicMock()
    p.stdout = payload
    p.stderr = ""
    p.returncode = returncode
    return p


def _with_binary(fn):
    def wrapper(*a, **kw):
        with patch(
            "bizniz.agents.debugger.claude_cli_debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            return fn(*a, **kw)
    return wrapper


class TestConstruction:
    def test_raises_when_binary_missing(self):
        with patch(
            "bizniz.agents.debugger.claude_cli_debugger.shutil.which",
            return_value=None,
        ):
            with pytest.raises(AgenticDebuggerError) as exc:
                ClaudeCliDebugger()
            assert "not on PATH" in str(exc.value)

    @_with_binary
    def test_ok_when_binary_present(self):
        d = ClaudeCliDebugger(compose_path="/p/c.yml", service_name="backend")
        assert d._command == "claude"


class TestDiagnose:
    @_with_binary
    def test_returns_diagnosis_from_json_payload(self):
        d = ClaudeCliDebugger(compose_path="/p/c.yml", service_name="backend")
        diag_json = json.dumps({
            "diagnosis": "ImportError: app.foo missing",
            "root_cause_category": "import_error",
            "fix_target": "code",
            "affected_files": ["app/foo.py"],
            "fix_plan": ["create app/foo.py"],
            "suggested_approach": "Wrote the missing module",
            "missing_packages": [],
            "confidence": "high",
            "code_fixes": [],
        })
        with patch(
            "bizniz.agents.debugger.claude_cli_debugger.subprocess.run",
            return_value=_fake_proc(diag_json),
        ):
            out = d.diagnose(
                error_output="ImportError: ...",
                source_files={},
                test_files={},
            )
        assert isinstance(out, AgenticDiagnosis)
        assert out.root_cause_category == "import_error"
        assert out.confidence == "high"
        assert out.suggested_approach == "Wrote the missing module"

    @_with_binary
    def test_extracts_fenced_json(self):
        d = ClaudeCliDebugger(compose_path="/p/c.yml", service_name="backend")
        diag = {"diagnosis": "fixed", "root_cause_category": "logic_error"}
        text = f"I did the work.\n\n```json\n{json.dumps(diag)}\n```"
        with patch(
            "bizniz.agents.debugger.claude_cli_debugger.subprocess.run",
            return_value=_fake_proc(text),
        ):
            out = d.diagnose(error_output="x", source_files={}, test_files={})
        assert out.root_cause_category == "logic_error"

    @_with_binary
    def test_no_json_falls_back_to_low_confidence_stub(self):
        d = ClaudeCliDebugger(compose_path="/p/c.yml", service_name="backend")
        with patch(
            "bizniz.agents.debugger.claude_cli_debugger.subprocess.run",
            return_value=_fake_proc("I gave up; here's some prose."),
        ):
            out = d.diagnose(error_output="x", source_files={}, test_files={})
        assert out.confidence == "low"
        assert "did not emit" in out.diagnosis

    @_with_binary
    def test_invalid_fix_target_coerced_to_code(self):
        d = ClaudeCliDebugger(compose_path="/p/c.yml", service_name="backend")
        text = json.dumps({
            "diagnosis": "x", "fix_target": "wild_card_value",
        })
        with patch(
            "bizniz.agents.debugger.claude_cli_debugger.subprocess.run",
            return_value=_fake_proc(text),
        ):
            out = d.diagnose(error_output="x", source_files={}, test_files={})
        assert out.fix_target == "code"

    @_with_binary
    def test_command_includes_required_flags(self):
        d = ClaudeCliDebugger(compose_path="/p/c.yml", service_name="backend")
        text = json.dumps({"diagnosis": "x"})
        with patch(
            "bizniz.agents.debugger.claude_cli_debugger.subprocess.run",
            return_value=_fake_proc(text),
        ) as m:
            d.diagnose(error_output="x", source_files={}, test_files={})
        argv = m.call_args.args[0]
        assert "--print" in argv
        assert "--output-format=json" in argv
        assert "--permission-mode" in argv
        idx = argv.index("--permission-mode")
        assert argv[idx + 1] == "bypassPermissions"
        assert "--mcp-config" in argv

    @_with_binary
    def test_raises_on_non_zero_exit(self):
        d = ClaudeCliDebugger(compose_path="/p/c.yml", service_name="backend")
        with patch(
            "bizniz.agents.debugger.claude_cli_debugger.subprocess.run",
            return_value=_fake_proc("x", returncode=2),
        ):
            with pytest.raises(AgenticDebuggerError) as exc:
                d.diagnose(error_output="x", source_files={}, test_files={})
        assert "exited 2" in str(exc.value)

    @_with_binary
    def test_raises_on_timeout(self):
        d = ClaudeCliDebugger(compose_path="/p/c.yml", service_name="backend")
        with patch(
            "bizniz.agents.debugger.claude_cli_debugger.subprocess.run",
            side_effect=subprocess.TimeoutExpired("claude", 600),
        ):
            with pytest.raises(AgenticDebuggerTimeoutError):
                d.diagnose(error_output="x", source_files={}, test_files={})


class TestPromptShape:
    @_with_binary
    def test_prompt_carries_failure_and_workspace_info(self):
        d = ClaudeCliDebugger(
            compose_path="/p/c.yml", service_name="backend",
        )
        text = json.dumps({"diagnosis": "x"})
        with patch(
            "bizniz.agents.debugger.claude_cli_debugger.subprocess.run",
            return_value=_fake_proc(text),
        ) as m:
            d.diagnose(
                error_output="ImportError: foo",
                source_files={"app/foo.py": "..."},
                test_files={"tests/test_foo.py": "..."},
                repair_history=["attempt 1 stalled"],
            )
        stdin = m.call_args.kwargs["input"]
        assert "ImportError: foo" in stdin
        assert "backend" in stdin
        assert "app/foo.py" in stdin
        assert "tests/test_foo.py" in stdin
        assert "attempt 1 stalled" in stdin
        assert "mcp__bizniz" in stdin

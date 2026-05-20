"""Tests for ``PerIssueDebugger`` — v4 Option 3 tool-loop debugger.

Mocks the ``claude --print`` subprocess to verify command shape,
prompt construction, status-line parsing, and timeout/error paths.
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition
from bizniz.coder.types import Issue
from bizniz.coder_tester.types import FilledFile
from bizniz.per_issue_validator.debugger import (
    PerIssueDebugger, PerIssueDebuggerError, _truncate,
)
from bizniz.per_issue_validator.types import Finding
from bizniz.workspace.local_workspace import LocalWorkspace


def _service() -> ServiceDefinition:
    return ServiceDefinition(
        name="backend",
        service_type="backend",
        framework="fastapi",
        language="python",
        workspace_name="backend",
        port=8000,
        description="API",
        depends_on=[],
    )


def _issue() -> Issue:
    return Issue(
        id="BE-001",
        title="t",
        description="d",
        service="backend",
        language="python",
        target_files=["app/me.py"],
        test_files=["tests/test_me.py"],
        success_criteria=[],
        spec_refs=[],
        depends_on=[],
    )


def _workspace(tmp_path) -> LocalWorkspace:
    root = tmp_path / "ws"
    root.mkdir()
    return LocalWorkspace(root)


def _ok_proc(text: str) -> MagicMock:
    return MagicMock(
        returncode=0,
        stdout=json.dumps({"result": text, "session_id": "test-sess"}),
        stderr="",
    )


# ── Truncation ─────────────────────────────────────────────────────


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("short", 100) == "short"

    def test_long_text_truncated_in_middle(self):
        long = "a" * 1000 + "MIDDLE" + "b" * 1000
        out = _truncate(long, 500)
        assert len(out) < len(long)
        assert "truncated" in out
        assert out.startswith("a" * 100)  # head preserved
        assert out.endswith("b" * 100)    # tail preserved


# ── Init ───────────────────────────────────────────────────────────


class TestInit:
    def test_raises_when_claude_missing(self, tmp_path):
        with patch(
            "bizniz.per_issue_validator.debugger.shutil.which",
            return_value=None,
        ):
            with pytest.raises(PerIssueDebuggerError, match="not on PATH"):
                PerIssueDebugger(workspace=_workspace(tmp_path))


# ── Command shape ──────────────────────────────────────────────────


class TestCommandShape:
    def test_cmd_includes_full_tool_loop_args(self, tmp_path):
        with patch(
            "bizniz.per_issue_validator.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerIssueDebugger(
                workspace=_workspace(tmp_path),
                compose_path="/proj/c.yml",
                service_name="backend",
            )
        with patch(
            "bizniz.per_issue_validator.debugger.subprocess.run",
            return_value=_ok_proc("DEBUGGER_DONE: status=clean, files_touched=[app/me.py]"),
        ) as mock_run:
            dbg.debug(
                issue=_issue(),
                service=_service(),
                current_files=[FilledFile(path="app/me.py", content="x", role="code")],
                findings=[Finding(source="symbol_validator", message="m")],
                capabilities=[],
            )
        cmd = mock_run.call_args.args[0] if mock_run.call_args.args else mock_run.call_args.kwargs.get("args") or []
        # Defensive: subprocess.run may have been called with cmd as positional
        if not cmd:
            cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "claude" in cmd_str
        assert "--print" in cmd_str
        assert "--permission-mode" in cmd_str
        assert "bypassPermissions" in cmd_str
        assert "--allowed-tools" in cmd_str
        assert "Bash" in cmd_str
        assert "Edit" in cmd_str
        assert "Write" in cmd_str
        assert "Read" in cmd_str
        assert "--add-dir" in cmd_str


# ── Status parse ───────────────────────────────────────────────────


class TestStatusParse:
    def test_clean_status_returns_clean_validated_issue(self, tmp_path):
        with patch(
            "bizniz.per_issue_validator.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerIssueDebugger(workspace=_workspace(tmp_path))
        text = (
            "Investigated the issue, fixed the import.\n\n"
            "DEBUGGER_DONE: status=clean, files_touched=[app/me.py, tests/test_me.py]\n"
        )
        with patch(
            "bizniz.per_issue_validator.debugger.subprocess.run",
            return_value=_ok_proc(text),
        ):
            result = dbg.debug(
                issue=_issue(),
                service=_service(),
                current_files=[
                    FilledFile(path="app/me.py", content="x", role="code"),
                ],
                findings=[Finding(source="symbol_validator", message="m")],
                capabilities=[],
            )
        assert result.clean is True
        assert "app/me.py" in result.files_written
        assert "tests/test_me.py" in result.files_written

    def test_partial_status_returns_not_clean(self, tmp_path):
        with patch(
            "bizniz.per_issue_validator.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerIssueDebugger(workspace=_workspace(tmp_path))
        text = (
            "Made some progress but couldn't resolve everything.\n"
            "DEBUGGER_DONE: status=partial, files_touched=[app/me.py]\n"
        )
        with patch(
            "bizniz.per_issue_validator.debugger.subprocess.run",
            return_value=_ok_proc(text),
        ):
            result = dbg.debug(
                issue=_issue(),
                service=_service(),
                current_files=[FilledFile(path="app/me.py", content="x", role="code")],
                findings=[Finding(source="symbol_validator", message="m")],
                capabilities=[],
            )
        assert result.clean is False
        assert "debugger_partial" in result.halt_reason

    def test_missing_status_line_defaults_to_partial(self, tmp_path):
        with patch(
            "bizniz.per_issue_validator.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerIssueDebugger(workspace=_workspace(tmp_path))
        text = "I made some edits. (no DEBUGGER_DONE line)"
        with patch(
            "bizniz.per_issue_validator.debugger.subprocess.run",
            return_value=_ok_proc(text),
        ):
            result = dbg.debug(
                issue=_issue(),
                service=_service(),
                current_files=[FilledFile(path="app/me.py", content="x", role="code")],
                findings=[Finding(source="ast", message="m")],
                capabilities=[],
            )
        assert result.clean is False


# ── Timeout + errors ──────────────────────────────────────────────


class TestErrorPaths:
    def test_timeout_returns_partial_with_halt_reason(self, tmp_path):
        with patch(
            "bizniz.per_issue_validator.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerIssueDebugger(
                workspace=_workspace(tmp_path),
                timeout_seconds=1,
            )
        with patch(
            "bizniz.per_issue_validator.debugger.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1),
        ):
            result = dbg.debug(
                issue=_issue(),
                service=_service(),
                current_files=[FilledFile(path="app/me.py", content="x", role="code")],
                findings=[Finding(source="ast", message="m")],
                capabilities=[],
            )
        assert result.clean is False
        assert result.halt_reason == "debugger_timeout"

    def test_non_zero_exit_returns_partial(self, tmp_path):
        with patch(
            "bizniz.per_issue_validator.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerIssueDebugger(workspace=_workspace(tmp_path))
        proc = MagicMock(returncode=2, stdout="", stderr="claude crashed")
        with patch(
            "bizniz.per_issue_validator.debugger.subprocess.run",
            return_value=proc,
        ):
            result = dbg.debug(
                issue=_issue(),
                service=_service(),
                current_files=[FilledFile(path="app/me.py", content="x", role="code")],
                findings=[Finding(source="ast", message="m")],
                capabilities=[],
            )
        assert result.clean is False
        assert "debugger_exit_2" in result.halt_reason


# ── Prompt construction ───────────────────────────────────────────


class TestPromptConstruction:
    def test_prompt_includes_issue_findings_files_compose(self, tmp_path):
        with patch(
            "bizniz.per_issue_validator.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerIssueDebugger(
                workspace=_workspace(tmp_path),
                compose_path="/proj/c.yml",
                service_name="backend",
            )
        with patch(
            "bizniz.per_issue_validator.debugger.subprocess.run",
            return_value=_ok_proc("DEBUGGER_DONE: status=clean, files_touched=[]"),
        ) as mock_run:
            dbg.debug(
                issue=_issue(),
                service=_service(),
                current_files=[FilledFile(path="app/me.py", content="raise NotImplementedError", role="code")],
                findings=[
                    Finding(source="symbol_validator", message="unresolved import: foo"),
                    Finding(source="ast", message="syntax error"),
                ],
                capabilities=[],
            )
        prompt = mock_run.call_args.kwargs.get("input") or ""
        assert "BE-001" in prompt
        assert "unresolved import: foo" in prompt
        assert "syntax error" in prompt
        assert "app/me.py" in prompt
        assert "raise NotImplementedError" in prompt
        assert "/proj/c.yml" in prompt
        assert "DEBUGGER_DONE" in prompt

"""Tests for ClaudeCliCoder.

Live CLI calls are deferred to ``@pytest.mark.functional``. These
tests mock subprocess and verify command shape, prompt assembly,
and CoderResult parsing.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.coder.claude_cli_coder import ClaudeCliCoder
from bizniz.coder.types import CoderError, Issue
from bizniz.quality_engineer.types import EnrichedSpec


def _issue() -> Issue:
    return Issue(
        id="BE-001",
        title="Create user model",
        description="...",
        service="backend",
        language="python",
        target_files=["app/models/user.py"],
        test_files=["tests/test_user.py"],
    )


def _arch() -> SystemArchitecture:
    return SystemArchitecture(
        project_name="t",
        project_slug="t",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend",
                framework="fastapi", language="python",
                description="", workspace_name="backend",
                port=8000,
            ),
        ],
        description="",
    )


def _spec() -> EnrichedSpec:
    return EnrichedSpec(
        problem_statement="x",
        milestone_name="M1",
        milestone_description="d",
        capabilities=[],
    )


def _fake_proc(result_text: str, returncode: int = 0):
    payload = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result_text,
        "session_id": "sid-42",
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })
    p = MagicMock()
    p.stdout = payload
    p.stderr = ""
    p.returncode = returncode
    return p


def _with_binary(fn):
    """Decorator: pretend claude is installed on PATH."""
    def wrapper(*a, **kw):
        with patch(
            "bizniz.coder.claude_cli_coder.shutil.which",
            return_value="/usr/bin/claude",
        ):
            return fn(*a, **kw)
    return wrapper


class TestConstruction:
    def test_raises_when_binary_missing(self):
        with patch(
            "bizniz.coder.claude_cli_coder.shutil.which",
            return_value=None,
        ):
            with pytest.raises(CoderError) as exc:
                ClaudeCliCoder(target_service="backend", compose_path="/p/c.yml")
            assert "not on PATH" in str(exc.value)

    @_with_binary
    def test_ok_when_binary_present(self):
        c = ClaudeCliCoder(target_service="backend", compose_path="/p/c.yml")
        assert c._command == "claude"


class TestCodeIssueSubprocess:
    @_with_binary
    def test_returns_coder_result_from_json_payload(self):
        c = ClaudeCliCoder(target_service="backend", compose_path="/p/c.yml")
        result_json = json.dumps({
            "issue_id": "BE-001",
            "status": "passed",
            "target_files_written": ["app/models/user.py"],
            "test_files_written": ["tests/test_user.py"],
            "summary": "ok",
            "notes": [],
        })
        with patch(
            "bizniz.coder.claude_cli_coder.subprocess.run",
            return_value=_fake_proc(result_json),
        ):
            out = c.code_issue(_issue(), _arch(), _spec())
        assert out.issue_id == "BE-001"
        assert out.status == "passed"
        assert out.target_files_written == ["app/models/user.py"]

    @_with_binary
    def test_extracts_fenced_json(self):
        c = ClaudeCliCoder(target_service="backend", compose_path="/p/c.yml")
        fenced = (
            "I did the work.\n\n```json\n"
            + json.dumps({"issue_id": "BE-001", "status": "passed"})
            + "\n```"
        )
        with patch(
            "bizniz.coder.claude_cli_coder.subprocess.run",
            return_value=_fake_proc(fenced),
        ):
            out = c.code_issue(_issue(), _arch(), _spec())
        assert out.status == "passed"

    @_with_binary
    def test_extracts_trailing_json_after_prose(self):
        c = ClaudeCliCoder(target_service="backend", compose_path="/p/c.yml")
        trailing = (
            "Some preamble.\nMore words.\n"
            + json.dumps({"issue_id": "BE-001", "status": "partial"})
        )
        with patch(
            "bizniz.coder.claude_cli_coder.subprocess.run",
            return_value=_fake_proc(trailing),
        ):
            out = c.code_issue(_issue(), _arch(), _spec())
        assert out.status == "partial"

    @_with_binary
    def test_falls_back_to_partial_on_no_json(self):
        c = ClaudeCliCoder(target_service="backend", compose_path="/p/c.yml")
        with patch(
            "bizniz.coder.claude_cli_coder.subprocess.run",
            return_value=_fake_proc("I gave up, sorry."),
        ):
            out = c.code_issue(_issue(), _arch(), _spec())
        assert out.status == "partial"
        assert out.issue_id == "BE-001"
        assert "did not emit" in out.summary

    @_with_binary
    def test_command_includes_required_flags(self):
        c = ClaudeCliCoder(target_service="backend", compose_path="/p/c.yml")
        result_json = json.dumps({"issue_id": "BE-001", "status": "passed"})
        with patch(
            "bizniz.coder.claude_cli_coder.subprocess.run",
            return_value=_fake_proc(result_json),
        ) as m:
            c.code_issue(_issue(), _arch(), _spec())
        argv = m.call_args.args[0]
        assert "--print" in argv
        assert "--output-format=json" in argv
        assert "--permission-mode" in argv
        idx = argv.index("--permission-mode")
        assert argv[idx + 1] == "bypassPermissions"
        assert "--allowed-tools" in argv
        idx = argv.index("--allowed-tools")
        # Allowed tools list should at minimum include Edit, Write, Bash
        tools = argv[idx + 1]
        for t in ("Edit", "Write", "Bash", "Read"):
            assert t in tools

    @_with_binary
    def test_raises_on_non_zero_exit(self):
        c = ClaudeCliCoder(target_service="backend", compose_path="/p/c.yml")
        with patch(
            "bizniz.coder.claude_cli_coder.subprocess.run",
            return_value=_fake_proc("x", returncode=2),
        ):
            with pytest.raises(CoderError) as exc:
                c.code_issue(_issue(), _arch(), _spec())
        assert "exited 2" in str(exc.value)

    @_with_binary
    def test_raises_on_timeout(self):
        c = ClaudeCliCoder(target_service="backend", compose_path="/p/c.yml")
        with patch(
            "bizniz.coder.claude_cli_coder.subprocess.run",
            side_effect=subprocess.TimeoutExpired("claude", 60),
        ):
            with pytest.raises(CoderError) as exc:
                c.code_issue(_issue(), _arch(), _spec())
        assert "timed out" in str(exc.value)


class TestPromptShape:
    @_with_binary
    def test_prompt_carries_workspace_and_runner_instructions(self):
        c = ClaudeCliCoder(
            target_service="backend",
            compose_path="/p/c.yml",
            workspace_name="backend",
            runner="pytest",
        )
        result_json = json.dumps({"issue_id": "BE-001", "status": "passed"})
        with patch(
            "bizniz.coder.claude_cli_coder.subprocess.run",
            return_value=_fake_proc(result_json),
        ) as m:
            c.code_issue(_issue(), _arch(), _spec())
        stdin = m.call_args.kwargs["input"]
        assert "BE-001" in stdin
        assert "Create user model" in stdin
        assert "docker compose -f /p/c.yml exec" in stdin
        assert "pytest" in stdin


class TestExtractJsonObject:
    def test_returns_none_on_empty(self):
        assert ClaudeCliCoder._extract_json_object("") is None

    def test_returns_whole_string_if_bare_object(self):
        s = '{"a": 1}'
        assert ClaudeCliCoder._extract_json_object(s) == s

    def test_returns_contents_of_fenced_block(self):
        out = ClaudeCliCoder._extract_json_object(
            "prose\n```json\n{\"a\": 1}\n```\nmore"
        )
        assert out == '{"a": 1}'

    def test_balanced_brace_scan_handles_nested(self):
        out = ClaudeCliCoder._extract_json_object(
            "ignored {trash}\nfinal {\"a\": {\"b\": 1}}"
        )
        assert out == '{"a": {"b": 1}}'

"""Tests for PerMilestoneDebugger."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bizniz.canonical_findings.types import CanonicalFinding
from bizniz.per_milestone_debugger.debugger import (
    PerMilestoneDebugger, PerMilestoneDebuggerError, _truncate,
)


def _findings(n: int = 2):
    return [
        CanonicalFinding(
            id=f"f{i}", source="quality_engineer",
            priority="critical",
            summary=f"finding {i}", file_hint=f"app/{i}.py",
        )
        for i in range(n)
    ]


def _ok_proc(text: str):
    return MagicMock(
        returncode=0,
        stdout=json.dumps({"result": text, "session_id": "sess"}),
        stderr="",
    )


# ── Init ───────────────────────────────────────────────────────────


class TestInit:
    def test_raises_when_claude_missing(self, tmp_path):
        with patch(
            "bizniz.per_milestone_debugger.debugger.shutil.which",
            return_value=None,
        ):
            with pytest.raises(PerMilestoneDebuggerError, match="not on PATH"):
                PerMilestoneDebugger(project_root=tmp_path)


# ── Empty findings ────────────────────────────────────────────────


class TestEmptyFindings:
    def test_no_findings_returns_clean_zero_wall(self, tmp_path):
        with patch(
            "bizniz.per_milestone_debugger.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerMilestoneDebugger(project_root=tmp_path)
        result = dbg.debug(milestone_name="M1", findings=[])
        assert result.clean is True
        assert result.files_touched == []


# ── Command shape ──────────────────────────────────────────────────


class TestCommandShape:
    def test_uses_milestone_scope_tools(self, tmp_path):
        with patch(
            "bizniz.per_milestone_debugger.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerMilestoneDebugger(
                project_root=tmp_path,
                compose_path="/proj/c.yml",
            )
        with patch(
            "bizniz.per_milestone_debugger.debugger.subprocess.run",
            return_value=_ok_proc(
                "DEBUGGER_DONE: status=clean, files_touched=[app/x.py]"
            ),
        ) as mock_run:
            dbg.debug(milestone_name="M1", findings=_findings(2))
        cmd = mock_run.call_args.args[0]
        cmd_str = " ".join(cmd)
        assert "claude" in cmd_str
        assert "--print" in cmd_str
        assert "--permission-mode" in cmd_str
        assert "bypassPermissions" in cmd_str
        assert "Bash" in cmd_str
        assert "Edit" in cmd_str
        assert "Write" in cmd_str
        # Added the PROJECT ROOT (not a single service workspace) — that's
        # what makes this milestone-scoped, not issue-scoped.
        assert "--add-dir" in cmd_str
        assert str(tmp_path) in cmd_str


# ── Status parse ───────────────────────────────────────────────────


class TestStatusParse:
    def test_clean_status_returns_clean(self, tmp_path):
        with patch(
            "bizniz.per_milestone_debugger.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerMilestoneDebugger(project_root=tmp_path)
        text = (
            "Fixed cross-service issue.\n"
            "DEBUGGER_DONE: status=clean, files_touched=[a.py, b.py]\n"
        )
        with patch(
            "bizniz.per_milestone_debugger.debugger.subprocess.run",
            return_value=_ok_proc(text),
        ):
            result = dbg.debug(milestone_name="M1", findings=_findings(1))
        assert result.clean is True
        assert set(result.files_touched) == {"a.py", "b.py"}

    def test_partial_status_returns_not_clean(self, tmp_path):
        with patch(
            "bizniz.per_milestone_debugger.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerMilestoneDebugger(project_root=tmp_path)
        with patch(
            "bizniz.per_milestone_debugger.debugger.subprocess.run",
            return_value=_ok_proc(
                "DEBUGGER_DONE: status=partial, files_touched=[c.py]"
            ),
        ):
            result = dbg.debug(milestone_name="M1", findings=_findings(2))
        assert result.clean is False
        assert "partial" in result.halt_reason


# ── Timeout + error ────────────────────────────────────────────────


class TestErrorPaths:
    def test_timeout_returns_not_clean(self, tmp_path):
        with patch(
            "bizniz.per_milestone_debugger.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerMilestoneDebugger(
                project_root=tmp_path, timeout_seconds=1,
            )
        with patch(
            "bizniz.per_milestone_debugger.debugger.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1),
        ):
            result = dbg.debug(milestone_name="M1", findings=_findings(1))
        assert result.clean is False
        assert result.halt_reason == "timeout"

    def test_non_zero_exit_returns_not_clean(self, tmp_path):
        with patch(
            "bizniz.per_milestone_debugger.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerMilestoneDebugger(project_root=tmp_path)
        proc = MagicMock(returncode=2, stdout="", stderr="boom")
        with patch(
            "bizniz.per_milestone_debugger.debugger.subprocess.run",
            return_value=proc,
        ):
            result = dbg.debug(milestone_name="M1", findings=_findings(1))
        assert result.clean is False
        assert "exit_2" in result.halt_reason


# ── Prompt construction ────────────────────────────────────────────


class TestPromptConstruction:
    def test_prompt_includes_compose_and_findings(self, tmp_path):
        with patch(
            "bizniz.per_milestone_debugger.debugger.shutil.which",
            return_value="/usr/bin/claude",
        ):
            dbg = PerMilestoneDebugger(
                project_root=tmp_path,
                compose_path="/proj/c.yml",
            )
        with patch(
            "bizniz.per_milestone_debugger.debugger.subprocess.run",
            return_value=_ok_proc(
                "DEBUGGER_DONE: status=clean, files_touched=[]"
            ),
        ) as mock_run:
            dbg.debug(milestone_name="M1", findings=_findings(3))
        prompt = mock_run.call_args.kwargs.get("input") or ""
        assert "M1" in prompt
        assert "/proj/c.yml" in prompt
        # All findings rendered.
        for f in _findings(3):
            assert f.id in prompt
        assert "DEBUGGER_DONE" in prompt


# ── Truncation helper ─────────────────────────────────────────────


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("short", 100) == "short"

    def test_long_text_truncated_in_middle(self):
        long = "a" * 1000 + "b" * 1000
        out = _truncate(long, 500)
        assert "truncated" in out
        assert len(out) < len(long)

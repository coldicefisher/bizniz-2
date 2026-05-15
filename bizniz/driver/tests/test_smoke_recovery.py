"""Tests for SmokeRecovery — one-shot agent that tries to fix smoke
phase failures before the pipeline hard-halts.

Covers the parser for ACTION:/RECOVERY-{SUCCESS,FAILED} lines, the
no-claude-binary skip path, and the dispatch shape (cmd contains the
right flags). The actual claude subprocess is mocked — the real
recovery path is exercised live during pipeline runs.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.driver.smoke_recovery import SmokeRecovery, SmokeRecoveryResult


def _make(**kwargs):
    defaults = dict(
        compose_path="/tmp/compose.yml",
        project_root=Path("/tmp"),
    )
    defaults.update(kwargs)
    return SmokeRecovery(**defaults)


class TestExtractActions:
    def test_pulls_action_lines(self):
        text = (
            "I'll restart the backend.\n"
            "ACTION: docker compose restart backend\n"
            "Now verifying the route.\n"
            "ACTION: curl /api/v1/contacts → 200\n"
            "RECOVERY SUCCESS: tables created on lifespan re-run.\n"
        )
        actions = SmokeRecovery._extract_actions(text)
        assert actions == [
            "docker compose restart backend",
            "curl /api/v1/contacts → 200",
        ]

    def test_no_actions_returns_empty(self):
        text = "Could not diagnose. RECOVERY FAILED: needs human."
        actions = SmokeRecovery._extract_actions(text)
        assert actions == []

    def test_case_insensitive_action(self):
        text = "action: did the thing\nAction: did the other"
        actions = SmokeRecovery._extract_actions(text)
        assert len(actions) == 2

    def test_truncates_long_actions(self):
        text = "ACTION: " + ("x" * 500)
        actions = SmokeRecovery._extract_actions(text)
        assert len(actions[0]) <= 200


class TestRecoverFlow:
    def test_no_claude_binary_skips_attempt(self):
        with patch(
            "bizniz.driver.smoke_recovery.shutil.which",
            return_value=None,
        ):
            r = _make()
            out = r.recover(
                critical_failures=["route[/x] 500"],
                service_names=["backend"],
                milestone_title="M3",
            )
        assert out.attempted is False
        assert out.succeeded is False
        assert "claude binary not available" in out.summary

    def test_recovery_success_when_model_reports_ok(self):
        success_stdout = json.dumps({
            "result": (
                "Diagnosed missing tables.\n"
                "ACTION: docker compose restart backend\n"
                "ACTION: curl /api/v1/contacts → 200\n"
                "RECOVERY SUCCESS: backend restarted, tables created"
            ),
            "session_id": "s",
        })
        with patch(
            "bizniz.driver.smoke_recovery.shutil.which",
            return_value="/usr/bin/claude",
        ), patch(
            "bizniz.driver.smoke_recovery.subprocess.run"
        ) as run:
            run.return_value = MagicMock(
                returncode=0, stdout=success_stdout, stderr="",
            )
            r = _make()
            out = r.recover(
                critical_failures=["route[/x] 500"],
                service_names=["backend"],
                milestone_title="M3",
            )
        assert out.attempted is True
        assert out.succeeded is True
        assert "docker compose restart backend" in out.actions_taken[0]
        assert run.call_count == 1
        # Verify the cmd has the right shape.
        cmd = run.call_args.args[0]
        assert "--print" in cmd
        assert "--add-dir" in cmd
        assert "Bash" in " ".join(cmd)  # allowed tools include Bash

    def test_recovery_failure_when_model_reports_failed(self):
        failed_stdout = json.dumps({
            "result": (
                "Cannot diagnose without more context.\n"
                "RECOVERY FAILED: missing database password env var"
            ),
            "session_id": "s",
        })
        with patch(
            "bizniz.driver.smoke_recovery.shutil.which",
            return_value="/usr/bin/claude",
        ), patch(
            "bizniz.driver.smoke_recovery.subprocess.run"
        ) as run:
            run.return_value = MagicMock(
                returncode=0, stdout=failed_stdout, stderr="",
            )
            r = _make()
            out = r.recover(
                critical_failures=["route[/x] 500"],
                service_names=["backend"],
                milestone_title="M3",
            )
        assert out.attempted is True
        assert out.succeeded is False

    def test_is_error_returns_failure(self):
        with patch(
            "bizniz.driver.smoke_recovery.shutil.which",
            return_value="/usr/bin/claude",
        ), patch(
            "bizniz.driver.smoke_recovery.subprocess.run"
        ) as run:
            run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "is_error": True,
                    "result": "internal error",
                }),
                stderr="",
            )
            r = _make()
            out = r.recover(
                critical_failures=["route[/x] 500"],
                service_names=["backend"],
                milestone_title="M3",
            )
        assert out.attempted is True
        assert out.succeeded is False
        assert "is_error" in out.summary

    def test_fallback_model_appended_when_set(self):
        with patch(
            "bizniz.driver.smoke_recovery.shutil.which",
            return_value="/usr/bin/claude",
        ), patch(
            "bizniz.driver.smoke_recovery.subprocess.run"
        ) as run:
            run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "result": "RECOVERY SUCCESS",
                    "session_id": "s",
                }),
                stderr="",
            )
            r = _make(fallback_model="claude-haiku-4-5")
            r.recover(
                critical_failures=["route[/x] 500"],
                service_names=["backend"],
                milestone_title="M3",
            )
            cmd = run.call_args.args[0]
        assert "--fallback-model" in cmd
        idx = cmd.index("--fallback-model")
        assert cmd[idx + 1] == "claude-haiku-4-5"

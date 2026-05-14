"""Tests for the Refactorer agent. Subprocess mocked — live runs
deferred to @pytest.mark.functional."""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.planner.types import Milestone
from bizniz.refactorer.refactorer import Refactorer, RefactorerError


def _arch():
    return SystemArchitecture(
        project_name="t", project_slug="t", description="d",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend",
                framework="fastapi", language="python",
                description="", workspace_name="backend", port=8000,
            ),
            ServiceDefinition(
                name="frontend", service_type="frontend",
                framework="react", language="typescript",
                description="", workspace_name="frontend", port=5173,
            ),
        ],
    )


def _milestone():
    return Milestone(
        sequence_index=1, name="M2", problem_slice="x",
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
    """Decorator: pretend claude is installed on PATH. Wraps the
    fixture-aware test so pytest's argument injection still works."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with patch(
            "bizniz.refactorer.refactorer.shutil.which",
            return_value="/usr/bin/claude",
        ):
            return fn(*a, **kw)
    return wrapper


class TestConstruction:
    def test_raises_when_binary_missing(self, tmp_path):
        with patch(
            "bizniz.refactorer.refactorer.shutil.which",
            return_value=None,
        ):
            with pytest.raises(RefactorerError) as exc:
                Refactorer(project_root=tmp_path, compose_path="/p/c.yml")
            assert "not on PATH" in str(exc.value)

    @_with_binary
    def test_ok_when_binary_present(self, tmp_path):
        r = Refactorer(project_root=tmp_path, compose_path="/p/c.yml")
        assert r._command == "claude"


class TestRunParse:
    @_with_binary
    def test_parses_well_formed_result(self, tmp_path):
        r = Refactorer(project_root=tmp_path, compose_path="/p/c.yml")
        result_json = json.dumps({
            "status": "passed",
            "extractions": [{
                "name": "auth_headers",
                "shared_path": "shared/python/t_shared/auth.py",
                "consumers": ["backend"],
                "before": "duplicated header construction",
                "after": "shared helper",
                "tests_passed": True,
            }],
            "skipped": [],
            "summary": "extracted 1 helper",
            "notes": [],
        })
        with patch(
            "bizniz.refactorer.refactorer.subprocess.run",
            return_value=_fake_proc(result_json),
        ):
            out = r.run(_milestone(), _arch(), is_final_milestone=False)
        assert out.status == "passed"
        assert len(out.extractions) == 1
        assert out.extractions[0].name == "auth_headers"
        assert out.extractions[0].tests_passed is True

    @_with_binary
    def test_parses_no_op_outcome(self, tmp_path):
        r = Refactorer(project_root=tmp_path, compose_path="/p/c.yml")
        result_json = json.dumps({
            "status": "no_op",
            "extractions": [],
            "skipped": [],
            "summary": "scanned; nothing duplicated yet",
            "notes": [],
        })
        with patch(
            "bizniz.refactorer.refactorer.subprocess.run",
            return_value=_fake_proc(result_json),
        ):
            out = r.run(_milestone(), _arch(), is_final_milestone=False)
        assert out.status == "no_op"
        assert out.extractions == []

    @_with_binary
    def test_falls_back_to_no_op_on_unparseable(self, tmp_path):
        r = Refactorer(project_root=tmp_path, compose_path="/p/c.yml")
        with patch(
            "bizniz.refactorer.refactorer.subprocess.run",
            return_value=_fake_proc("just a sentence, no json here"),
        ):
            out = r.run(_milestone(), _arch(), is_final_milestone=False)
        assert out.status == "no_op"
        assert "did not emit" in out.summary

    @_with_binary
    def test_extracts_trailing_json_after_prose(self, tmp_path):
        r = Refactorer(project_root=tmp_path, compose_path="/p/c.yml")
        with_prose = (
            "I scanned the services and found nothing worth extracting.\n\n"
            + json.dumps({"status": "no_op", "summary": "clean", "notes": []})
        )
        with patch(
            "bizniz.refactorer.refactorer.subprocess.run",
            return_value=_fake_proc(with_prose),
        ):
            out = r.run(_milestone(), _arch(), is_final_milestone=False)
        assert out.status == "no_op"
        assert out.summary == "clean"

    @_with_binary
    def test_returns_failed_on_non_zero_exit(self, tmp_path):
        r = Refactorer(project_root=tmp_path, compose_path="/p/c.yml")
        bad = _fake_proc("oops", returncode=2)
        bad.stderr = "boom"
        with patch(
            "bizniz.refactorer.refactorer.subprocess.run",
            return_value=bad,
        ):
            out = r.run(_milestone(), _arch(), is_final_milestone=False)
        assert out.status == "failed"
        assert "exited 2" in out.summary

    @_with_binary
    def test_returns_partial_on_timeout(self, tmp_path):
        r = Refactorer(project_root=tmp_path, compose_path="/p/c.yml")
        with patch(
            "bizniz.refactorer.refactorer.subprocess.run",
            side_effect=subprocess.TimeoutExpired("claude", 60),
        ):
            out = r.run(_milestone(), _arch(), is_final_milestone=False)
        assert out.status == "partial"
        assert "Timed out" in out.summary


class TestArgvShape:
    @_with_binary
    def test_command_includes_required_flags(self, tmp_path):
        r = Refactorer(project_root=tmp_path, compose_path="/p/c.yml")
        with patch(
            "bizniz.refactorer.refactorer.subprocess.run",
            return_value=_fake_proc('{"status":"no_op","summary":"ok","notes":[]}'),
        ) as m:
            r.run(_milestone(), _arch(), is_final_milestone=False)
        argv = m.call_args.args[0]
        assert "--print" in argv
        assert "--output-format=json" in argv
        assert "--add-dir" in argv
        assert "--permission-mode" in argv
        idx = argv.index("--permission-mode")
        assert argv[idx + 1] == "bypassPermissions"

    @_with_binary
    def test_final_milestone_note_in_prompt(self, tmp_path):
        r = Refactorer(project_root=tmp_path, compose_path="/p/c.yml")
        with patch(
            "bizniz.refactorer.refactorer.subprocess.run",
            return_value=_fake_proc('{"status":"no_op","summary":"ok","notes":[]}'),
        ) as m:
            r.run(_milestone(), _arch(), is_final_milestone=True)
        prompt = m.call_args.kwargs["input"]
        assert "FINAL milestone" in prompt or "comprehensive" in prompt

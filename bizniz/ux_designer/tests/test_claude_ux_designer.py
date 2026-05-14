"""Tests for ClaudeUXDesigner — subprocess mocked.

Live CLI calls deferred to @pytest.mark.functional.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition
from bizniz.ux_designer.claude_ux_designer import ClaudeUXDesigner


def _frontend():
    return ServiceDefinition(
        name="frontend", service_type="frontend", framework="react",
        language="typescript", description="UI",
        workspace_name="frontend", port=5173,
    )


def _fake_proc(result_text: str, returncode: int = 0, is_error: bool = False):
    payload = json.dumps({
        "type": "result",
        "is_error": is_error,
        "result": result_text,
        "session_id": "sid",
    })
    p = MagicMock()
    p.stdout = payload
    p.stderr = ""
    p.returncode = returncode
    return p


def _designer():
    """ClaudeUXDesigner constructed with a mocked vision_client.
    No 'which claude' check here — we mock subprocess at use site."""
    return ClaudeUXDesigner(
        vision_client=MagicMock(),
        on_status=None,
    )


def _screenshots(tmp_path, n=2):
    d = tmp_path / "screenshots"
    d.mkdir()
    paths = []
    for i in range(n):
        p = d / f"shot_{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG header bytes
        paths.append(p)
    return [{"name": p.stem, "path": p, "bytes": p.read_bytes()} for p in paths]


class TestEvaluateScreenshots:
    def test_empty_returns_neutral(self):
        d = _designer()
        out = d._evaluate_screenshots(
            screenshots=[], service=_frontend(),
            problem_statement="x", design_system="Tailwind",
        )
        assert out["overall_score"] == 5
        assert out["issues"] == []
        assert "no screenshots" in out["summary"].lower()

    def test_parses_well_formed_json(self, tmp_path):
        d = _designer()
        shots = _screenshots(tmp_path)
        eval_json = json.dumps({
            "overall_score": 7,
            "summary": "looks reasonable",
            "issues": [
                {"severity": "minor", "category": "spacing",
                 "description": "x", "fix_description": "y"},
            ],
        })
        with patch(
            "bizniz.ux_designer.claude_ux_designer.subprocess.run",
            return_value=_fake_proc(eval_json),
        ) as m:
            out = d._evaluate_screenshots(
                screenshots=shots, service=_frontend(),
                problem_statement="recipe app", design_system="Tailwind",
            )
        assert out["overall_score"] == 7
        assert len(out["issues"]) == 1
        argv = m.call_args.args[0]
        assert "--print" in argv
        assert "--add-dir" in argv
        idx = argv.index("--add-dir")
        # The dir we pass is the parent of the first screenshot.
        assert argv[idx + 1] == str(tmp_path / "screenshots")

    def test_parses_trailing_json_after_prose(self, tmp_path):
        d = _designer()
        shots = _screenshots(tmp_path)
        prose = (
            "I reviewed each screenshot in turn. Here is my eval:\n\n"
            + json.dumps({"overall_score": 4, "summary": "ok", "issues": []})
        )
        with patch(
            "bizniz.ux_designer.claude_ux_designer.subprocess.run",
            return_value=_fake_proc(prose),
        ):
            out = d._evaluate_screenshots(
                screenshots=shots, service=_frontend(),
                problem_statement="x", design_system="Tailwind",
            )
        assert out["overall_score"] == 4

    def test_falls_back_on_non_zero_exit(self, tmp_path):
        d = _designer()
        shots = _screenshots(tmp_path)
        bad = _fake_proc("nope", returncode=2)
        bad.stderr = "boom"
        with patch(
            "bizniz.ux_designer.claude_ux_designer.subprocess.run",
            return_value=bad,
        ):
            out = d._evaluate_screenshots(
                screenshots=shots, service=_frontend(),
                problem_statement="x", design_system="Tailwind",
            )
        assert out["overall_score"] == 5
        assert "failed" in out["summary"].lower()

    def test_falls_back_on_timeout(self, tmp_path):
        d = _designer()
        shots = _screenshots(tmp_path)
        with patch(
            "bizniz.ux_designer.claude_ux_designer.subprocess.run",
            side_effect=subprocess.TimeoutExpired("claude", 60),
        ):
            out = d._evaluate_screenshots(
                screenshots=shots, service=_frontend(),
                problem_statement="x", design_system="Tailwind",
            )
        assert out["overall_score"] == 5
        assert "timeout" in out["summary"].lower()

    def test_falls_back_on_unparseable(self, tmp_path):
        d = _designer()
        shots = _screenshots(tmp_path)
        with patch(
            "bizniz.ux_designer.claude_ux_designer.subprocess.run",
            return_value=_fake_proc("just a sentence."),
        ):
            out = d._evaluate_screenshots(
                screenshots=shots, service=_frontend(),
                problem_statement="x", design_system="Tailwind",
            )
        assert out["overall_score"] == 5
        assert "unparseable" in out["summary"].lower()


class TestParseEvalJson:
    def test_direct_object(self):
        out = ClaudeUXDesigner._parse_eval_json('{"overall_score": 8}')
        assert out == {"overall_score": 8}

    def test_returns_none_on_empty(self):
        assert ClaudeUXDesigner._parse_eval_json("") is None

    def test_extracts_fenced(self):
        text = "prose\n```json\n{\"overall_score\": 3}\n```"
        out = ClaudeUXDesigner._parse_eval_json(text)
        assert out == {"overall_score": 3}

    def test_trailing_balanced(self):
        text = "lots of words {ignore} more stuff {\"overall_score\": 9}"
        out = ClaudeUXDesigner._parse_eval_json(text)
        assert out == {"overall_score": 9}

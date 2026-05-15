"""Tests for ProUXDesigner _view_iteration capture-mismatch handling
(Ticket 2). When the captured page doesn't match the requested route,
the iteration short-circuits before eval+fix and marks the result
``not_reviewable`` so it doesn't pollute APP SCORE or the review
cache.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition
from bizniz.ux_designer.pro_ux_designer import ProUXDesigner


def _designer():
    with patch(
        "bizniz.ux_designer.claude_ux_designer.shutil.which",
        return_value="/usr/bin/claude",
    ):
        return ProUXDesigner(vision_client=MagicMock(), on_status=None)


def _service():
    return ServiceDefinition(
        name="frontend", service_type="frontend",
        framework="react", language="typescript",
        description="x", workspace_name="frontend",
        port=5173, depends_on=[], requirements=[], skeleton="react",
    )


def _shot_with_meta(tmp_path, name, meta):
    png = tmp_path / f"{name}.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / f"{name}.meta.json").write_text(json.dumps(meta))
    return {"name": name, "path": png, "bytes": b""}


class TestCaptureMismatchShortCircuit:
    def test_skips_eval_and_fix_on_mismatch(self, tmp_path):
        d = _designer()
        # Stub the capture so it returns a screenshot whose meta says
        # we landed on /admin/users when we asked for /admin.
        shot = _shot_with_meta(tmp_path, "admin", {
            "requested_route": "/admin",
            "final_pathname": "/admin/users",
        })
        d._take_view_screenshots = MagicMock(return_value=[shot])
        d._evaluate_view = MagicMock()
        d._apply_view_fixes = MagicMock()

        out = d._view_iteration(
            route="/admin", view_meta={}, view_label="admin",
            iteration=1, service=_service(), workspace=MagicMock(),
            compose_path="x", problem_statement="x",
            milestone_scope="", design_plan={},
            auth_contract=None, backend_url=None,
        )
        # Eval and fix were skipped — we don't waste 30s on the wrong page.
        d._evaluate_view.assert_not_called()
        d._apply_view_fixes.assert_not_called()
        # Result correctly marked.
        assert out["not_reviewable"] is True
        assert out["captured_correctly"] is False
        assert out["final_score"] is None
        assert out["initial_score"] is None
        assert out["stop"] is True
        assert "capture mismatch" in out["stop_reason"]
        assert "/admin/users" in out["capture_mismatch_reason"]

    def test_clean_capture_still_runs_eval(self, tmp_path):
        d = _designer()
        # Meta says we landed on /admin (exact match).
        shot = _shot_with_meta(tmp_path, "admin", {
            "requested_route": "/admin",
            "final_pathname": "/admin",
        })
        d._take_view_screenshots = MagicMock(return_value=[shot])
        d._evaluate_view = MagicMock(return_value={
            "overall_score": 8, "issues": [], "stop_recommendation": "stop",
        })

        out = d._view_iteration(
            route="/admin", view_meta={}, view_label="admin",
            iteration=1, service=_service(), workspace=MagicMock(),
            compose_path="x", problem_statement="x",
            milestone_scope="", design_plan={},
            auth_contract=None, backend_url=None,
        )
        d._evaluate_view.assert_called_once()
        assert out["not_reviewable"] is False
        assert out["captured_correctly"] is True
        assert out["final_score"] == 8

    def test_empty_screenshots_returns_no_screenshots(self):
        d = _designer()
        d._take_view_screenshots = MagicMock(return_value=[])
        d._evaluate_view = MagicMock()
        out = d._view_iteration(
            route="/admin", view_meta={}, view_label="admin",
            iteration=1, service=_service(), workspace=MagicMock(),
            compose_path="x", problem_statement="x",
            milestone_scope="", design_plan={},
            auth_contract=None, backend_url=None,
        )
        d._evaluate_view.assert_not_called()
        assert out["stop"] is True
        assert "no screenshots" in out["stop_reason"]
        # Not_reviewable isn't set here — different signal. Caller
        # treats "no screenshots" as a hard failure, not "wrong page".
        assert out.get("not_reviewable") is not True

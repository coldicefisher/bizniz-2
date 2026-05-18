"""ProUXDesigner + StorybookDriver wiring tests (D20, item 2).

Verifies:
- StorybookDriver can be passed in at construction.
- ``review_frontend`` invokes the driver when wired.
- ``result["storybook"]`` is populated from the driver's result.
- Driver crashes don't tank the rest of the UX phase.
- Driver omitted → no storybook loop runs, result has no storybook
  key (legacy compat — pre-D20 builds keep working).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.ux_designer.pro_ux_designer import ProUXDesigner
from bizniz.ux_designer.storybook_driver import StorybookRunResult
from bizniz.ux_designer.storybook_score import StorybookScore


def _designer(storybook_driver=None):
    """Build a ProUXDesigner with claude binary patched-present."""
    with patch(
        "bizniz.ux_designer.claude_ux_designer.shutil.which",
        return_value="/usr/bin/claude",
    ):
        return ProUXDesigner(
            vision_client=MagicMock(),
            coder_factory=lambda *_a, **_kw: MagicMock(),
            on_status=None,
            storybook_driver=storybook_driver,
        )


class TestConstructorWiring:
    def test_accepts_storybook_driver(self):
        sb = MagicMock()
        d = _designer(storybook_driver=sb)
        assert d._storybook_driver is sb

    def test_storybook_driver_defaults_to_none(self):
        d = _designer(storybook_driver=None)
        assert d._storybook_driver is None


class TestStorybookLoopDispatch:
    """Drive ProUXDesigner._review_frontend_inner directly with a
    fake storybook_driver and verify the loop runs.

    We patch every other heavy bit of review_frontend so this stays
    a focused wiring test, not an end-to-end."""

    def _ok_sb_result(self):
        return StorybookRunResult(
            catalog_size=4,
            duration_s=1.0,
            score=StorybookScore(
                covered=4, passing=4, mean=8.0, by_story={},
            ),
        )

    def test_driver_invoked_when_wired(self, tmp_path):
        sb = MagicMock()
        sb.run.return_value = self._ok_sb_result()
        d = _designer(storybook_driver=sb)
        d._run_storybook_loop(
            ws_root_path=tmp_path,
            design_lock=None,
            result={},
        )
        sb.run.assert_called_once()
        kwargs = sb.run.call_args.kwargs
        assert kwargs["frontend_root"] == tmp_path
        # Screenshots dir is created.
        assert kwargs["screenshots_dir"].exists()

    def test_result_carries_storybook_payload(self, tmp_path):
        sb = MagicMock()
        sb.run.return_value = self._ok_sb_result()
        d = _designer(storybook_driver=sb)
        result: dict = {}
        d._run_storybook_loop(
            ws_root_path=tmp_path, design_lock=None, result=result,
        )
        assert "storybook" in result
        assert result["storybook"]["score"]["passing"] == 4

    def test_driver_exception_doesnt_propagate(self, tmp_path):
        sb = MagicMock()
        sb.run.side_effect = RuntimeError("server crashed")
        d = _designer(storybook_driver=sb)
        result: dict = {}
        # No raise — exception is captured + logged + result records reason.
        d._run_storybook_loop(
            ws_root_path=tmp_path, design_lock=None, result=result,
        )
        assert "skipped_reason" in result["storybook"]
        assert "server crashed" in result["storybook"]["skipped_reason"]

    def test_no_driver_no_storybook_key(self, tmp_path):
        d = _designer(storybook_driver=None)
        result: dict = {}
        d._run_storybook_loop(
            ws_root_path=tmp_path, design_lock=None, result=result,
        )
        assert "storybook" not in result

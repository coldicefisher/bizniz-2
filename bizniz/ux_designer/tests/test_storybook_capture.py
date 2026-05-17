"""Tests for ``storybook_capture`` — orchestration around the sidecar JS."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from bizniz.ux_designer.storybook_capture import (
    CapturePlan,
    SidecarResult,
    StoryCaptureResult,
    _story_url,
    build_capture_plan,
    capture_stories,
)
from bizniz.ux_designer.storybook_discovery import StoryCatalog, StoryEntry


def _entry(story_id: str, name: str = "Default",
           title: str = "UI/X") -> StoryEntry:
    return StoryEntry(
        story_id=story_id, name=name, title=title,
        stories_file=Path("/tmp/X.stories.tsx"),
    )


def _catalog(*entries: StoryEntry, root: str = "/tmp/x") -> StoryCatalog:
    return StoryCatalog(frontend_root=Path(root), stories=list(entries))


class TestStoryUrl:
    def test_canonical_form(self):
        assert _story_url(
            "http://localhost:6006", "common-toast--default",
        ) == "http://localhost:6006/iframe.html?id=common-toast--default&viewMode=story"

    def test_trailing_slash_handled(self):
        # Trailing slash on the base URL must not double-slash the path.
        assert "//iframe.html" not in _story_url(
            "http://localhost:6006/", "x--y",
        )


class TestBuildCapturePlan:
    def test_one_story(self, tmp_path):
        cat = _catalog(_entry("common-toast--default", "Default", "Common/Toast"))
        plan = build_capture_plan(cat, "http://localhost:6006", tmp_path)
        assert len(plan.stories) == 1
        s = plan.stories[0]
        assert s["story_id"] == "common-toast--default"
        assert s["url"].endswith("?id=common-toast--default&viewMode=story")
        assert s["output_path"].endswith("common-toast--default.png")

    def test_multiple_stories_preserve_catalog_order(self, tmp_path):
        cat = _catalog(
            _entry("a--1"),
            _entry("a--2"),
            _entry("b--1"),
        )
        plan = build_capture_plan(cat, "http://x:6006", tmp_path)
        assert [s["story_id"] for s in plan.stories] == ["a--1", "a--2", "b--1"]

    def test_default_viewport(self, tmp_path):
        plan = build_capture_plan(
            _catalog(_entry("x--y")),
            "http://x:6006",
            tmp_path,
        )
        assert plan.viewport_width == 1280
        assert plan.viewport_height == 720

    def test_custom_viewport(self, tmp_path):
        plan = build_capture_plan(
            _catalog(_entry("x--y")),
            "http://x:6006",
            tmp_path,
            viewport_width=375,
            viewport_height=812,
        )
        assert plan.viewport_width == 375
        assert plan.viewport_height == 812


class TestCaptureStories:
    def _make_invoker(
        self,
        records: List[dict],
        exit_code: int = 0,
        extra_stderr: List[str] = None,
    ):
        """Build a sidecar_invoker that emits the given records as
        stdout JSON lines."""
        def _invoke(plan: CapturePlan, timeout_s: float) -> SidecarResult:
            return SidecarResult(
                exit_code=exit_code,
                stdout_lines=[json.dumps(r) for r in records],
                stderr_lines=extra_stderr or [],
            )
        return _invoke

    def test_all_succeed(self, tmp_path):
        # Sidecar reports success for both stories; PNG files exist.
        cat = _catalog(
            _entry("a--1", "Default"),
            _entry("a--2", "Stacked"),
        )
        # Pre-create the "captured" PNGs so the file-existence check
        # passes.
        png1 = tmp_path / "a--1.png"
        png2 = tmp_path / "a--2.png"
        png1.write_bytes(b"\x89PNG fake")
        png2.write_bytes(b"\x89PNG fake")

        invoker = self._make_invoker([
            {"story_id": "a--1", "success": True, "output_path": str(png1), "duration_ms": 800},
            {"story_id": "a--2", "success": True, "output_path": str(png2), "duration_ms": 700},
        ])
        results = capture_stories(
            cat, "http://localhost:6006", tmp_path,
            sidecar_invoker=invoker,
        )
        assert len(results) == 2
        assert all(r.success for r in results)
        assert results[0].screenshot_path == png1
        assert results[1].screenshot_path == png2

    def test_mixed_success_and_failure(self, tmp_path):
        cat = _catalog(_entry("a--ok"), _entry("a--bad"))
        png = tmp_path / "a--ok.png"
        png.write_bytes(b"\x89PNG fake")

        invoker = self._make_invoker([
            {"story_id": "a--ok", "success": True, "output_path": str(png)},
            {"story_id": "a--bad", "success": False, "error": "TimeoutError: navigation"},
        ])
        results = capture_stories(
            cat, "http://localhost:6006", tmp_path,
            sidecar_invoker=invoker,
        )
        ok, bad = results
        assert ok.success is True and ok.screenshot_path == png
        assert bad.success is False
        assert "Timeout" in bad.error

    def test_success_claim_without_file_demoted(self, tmp_path):
        # Sidecar said success but PNG isn't on disk — file is the
        # source of truth.
        cat = _catalog(_entry("a--lying"))
        missing = tmp_path / "a--lying.png"
        invoker = self._make_invoker([
            {"story_id": "a--lying", "success": True, "output_path": str(missing)},
        ])
        results = capture_stories(
            cat, "http://localhost:6006", tmp_path,
            sidecar_invoker=invoker,
        )
        assert results[0].success is False
        assert "PNG missing" in results[0].error
        assert results[0].screenshot_path is None

    def test_sidecar_skips_a_story(self, tmp_path):
        # Sidecar emits records for some stories but not all — the
        # missing one becomes a failure with a clear error.
        cat = _catalog(_entry("a--1"), _entry("a--2"))
        png = tmp_path / "a--1.png"
        png.write_bytes(b"\x89PNG fake")
        invoker = self._make_invoker([
            {"story_id": "a--1", "success": True, "output_path": str(png)},
        ])
        results = capture_stories(
            cat, "http://localhost:6006", tmp_path,
            sidecar_invoker=invoker,
        )
        assert results[0].success is True
        assert results[1].success is False
        assert "did not report" in results[1].error

    def test_empty_catalog_short_circuits(self, tmp_path):
        # No stories → no sidecar call.
        called = [0]
        def invoker(plan, timeout):
            called[0] += 1
            return SidecarResult()
        results = capture_stories(
            _catalog(), "http://localhost:6006", tmp_path,
            sidecar_invoker=invoker,
        )
        assert results == []
        assert called[0] == 0

    def test_sidecar_emits_garbage_lines_safely(self, tmp_path):
        # Non-JSON noise on stdout shouldn't crash the parser.
        cat = _catalog(_entry("a--1"))
        png = tmp_path / "a--1.png"
        png.write_bytes(b"\x89PNG fake")
        def invoker(plan, timeout):
            return SidecarResult(
                exit_code=0,
                stdout_lines=[
                    "Loading playwright...",
                    "(node:42) ExperimentalWarning",
                    json.dumps({"story_id": "a--1", "success": True, "output_path": str(png)}),
                    "trailing log line",
                ],
            )
        results = capture_stories(
            cat, "http://localhost:6006", tmp_path,
            sidecar_invoker=invoker,
        )
        assert results[0].success is True

    def test_output_dir_created(self, tmp_path):
        out = tmp_path / "nested" / "ux_shots"
        assert not out.exists()
        cat = _catalog(_entry("a--1"))
        # Sidecar returns nothing usable; we still want output_dir to
        # have been created so callers can write into it later.
        invoker = self._make_invoker([])
        capture_stories(
            cat, "http://localhost:6006", out,
            sidecar_invoker=invoker,
        )
        assert out.is_dir()

    def test_on_status_callback_invoked(self, tmp_path):
        cat = _catalog(_entry("a--1"))
        png = tmp_path / "a--1.png"
        png.write_bytes(b"\x89PNG fake")
        statuses: List[str] = []
        invoker = self._make_invoker([
            {"story_id": "a--1", "success": True, "output_path": str(png)},
        ])
        capture_stories(
            cat, "http://localhost:6006", tmp_path,
            sidecar_invoker=invoker,
            on_status=lambda m: statuses.append(m),
        )
        assert len(statuses) >= 2  # planning + summary
        assert any("planning" in s for s in statuses)
        assert any("captured" in s for s in statuses)

"""Tests for ``StorybookDriver`` — Phase 6 end-to-end orchestration."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from bizniz.ux_designer.storybook_capture import StoryCaptureResult
from bizniz.ux_designer.storybook_discovery import StoryCatalog, StoryEntry
from bizniz.ux_designer.storybook_driver import (
    StorybookDriver, StorybookRunResult, StoryRunRecord,
)
from bizniz.ux_designer.storybook_eval import (
    StoryEvalIssue, StoryEvalResult,
)
from bizniz.ux_designer.storybook_fix import StoryFixResult


def _entry(story_id: str = "common-toast--default",
           name: str = "Default",
           title: str = "Common/Toast") -> StoryEntry:
    return StoryEntry(
        story_id=story_id, name=name, title=title,
        component_name="Toast",
        component_file=Path("/tmp/Toast.tsx"),
        stories_file=Path("/tmp/Toast.stories.tsx"),
    )


def _catalog(*entries: StoryEntry) -> StoryCatalog:
    if not entries:
        return StoryCatalog(frontend_root=Path("/tmp/frontend"))
    return StoryCatalog(
        frontend_root=Path("/tmp/frontend"),
        stories=list(entries),
    )


def _capture(story_id: str, success: bool = True,
             tmp_path: Path = None) -> StoryCaptureResult:
    png = (tmp_path or Path("/tmp")) / f"{story_id}.png"
    if success and tmp_path is not None:
        png.write_bytes(b"\x89PNG fake")
    return StoryCaptureResult(
        story_id=story_id, name="x", title="y",
        screenshot_path=png if success else None,
        success=success,
    )


def _make_server_factory(spawn_succeeds: bool = True):
    """Fake server factory that records start/stop calls."""
    class _FakeServer:
        def __init__(self, **kwargs):
            self.started = False
            self.stopped = False
        @property
        def base_url(self):
            return "http://localhost:6006"
        def __enter__(self):
            if not spawn_succeeds:
                raise RuntimeError("server failed to spawn")
            self.started = True
            return self
        def __exit__(self, exc_type, exc, tb):
            self.stopped = True
    def factory(**kwargs):
        return _FakeServer(**kwargs)
    return factory


# ── Skipping paths ───────────────────────────────────────────────


class TestSkippingPaths:
    def test_empty_catalog_short_circuits(self, tmp_path):
        evaluator = MagicMock()
        fix_dispatcher = MagicMock()
        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=fix_dispatcher,
            discover_fn=lambda root: _catalog(),
            server_factory=_make_server_factory(),
        )
        result = driver.run(
            frontend_root=Path("/tmp/frontend"),
            screenshots_dir=tmp_path,
        )
        assert result.skipped_reason is not None
        assert "no stories" in result.skipped_reason
        assert result.catalog_size == 0
        # Neither the server nor the evaluator should have been called.
        evaluator.evaluate.assert_not_called()

    def test_server_failure_recorded(self, tmp_path):
        evaluator = MagicMock()
        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=MagicMock(),
            discover_fn=lambda root: _catalog(_entry()),
            server_factory=_make_server_factory(spawn_succeeds=False),
        )
        result = driver.run(
            frontend_root=Path("/tmp/frontend"),
            screenshots_dir=tmp_path,
        )
        assert result.skipped_reason is not None
        assert "aborted" in result.skipped_reason
        assert result.server_started is False
        # Evaluator wasn't called.
        evaluator.evaluate.assert_not_called()


# ── Happy path ───────────────────────────────────────────────────


class TestHappyPath:
    def test_single_story_one_iter_passes(self, tmp_path):
        entry = _entry()
        catalog = _catalog(entry)

        def capture_fn(catalog, storybook_base_url, output_dir,
                       on_status=None):
            return [_capture(s.story_id, tmp_path=tmp_path)
                    for s in catalog.stories]

        evaluator = MagicMock()
        evaluator.evaluate.return_value = StoryEvalResult(
            story_id=entry.story_id, name=entry.name, title=entry.title,
            overall_score=9,
            stop_recommendation="stop",
        )
        fix_dispatcher = MagicMock()

        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=fix_dispatcher,
            discover_fn=lambda root: catalog,
            server_factory=_make_server_factory(),
            capture_fn=capture_fn,
            acceptable_score=7,
        )
        result = driver.run(
            frontend_root=Path("/tmp/frontend"),
            screenshots_dir=tmp_path,
        )
        assert result.skipped_reason is None
        assert result.catalog_size == 1
        assert len(result.story_records) == 1
        assert result.story_records[0].final_score == 9
        assert result.score.mean == 9.0
        assert result.score.passing == 1
        # Fix not dispatched (score met threshold).
        fix_dispatcher.dispatch.assert_not_called()


# ── Iteration paths ──────────────────────────────────────────────


class TestIteration:
    def test_iter1_fails_then_iter2_passes(self, tmp_path):
        entry = _entry()
        catalog = _catalog(entry)

        captures_called: list = []
        def capture_fn(catalog, storybook_base_url, output_dir,
                       on_status=None):
            captures_called.append(len(catalog.stories))
            return [_capture(s.story_id, tmp_path=tmp_path)
                    for s in catalog.stories]

        # iter 1: low score + iterate. iter 2: high score + stop.
        eval_results = iter([
            StoryEvalResult(
                story_id=entry.story_id, name=entry.name, title=entry.title,
                overall_score=4,
                issues=[StoryEvalIssue(severity="major",
                                       description="needs work")],
                stop_recommendation="iterate",
            ),
            StoryEvalResult(
                story_id=entry.story_id, name=entry.name, title=entry.title,
                overall_score=8,
                stop_recommendation="stop",
            ),
        ])
        evaluator = MagicMock()
        evaluator.evaluate.side_effect = lambda **kw: next(eval_results)

        fix_dispatcher = MagicMock()
        fix_dispatcher.dispatch.return_value = StoryFixResult(
            story_id=entry.story_id, name=entry.name, title=entry.title,
            status="applied",
            files_written=["/tmp/Toast.tsx"],
        )

        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=fix_dispatcher,
            discover_fn=lambda root: catalog,
            server_factory=_make_server_factory(),
            capture_fn=capture_fn,
            acceptable_score=7,
            max_iterations=3,
        )
        result = driver.run(
            frontend_root=Path("/tmp/frontend"),
            screenshots_dir=tmp_path,
        )
        record = result.story_records[0]
        assert len(record.iterations) == 2
        assert record.iterations[0].overall_score == 4
        assert record.iterations[1].overall_score == 8
        assert len(record.fixes_applied) == 1
        assert record.final_score == 8
        assert record.final_stop_reason == "score_met_threshold"
        # Captures: initial multi-story + one recapture.
        assert captures_called == [1, 1]

    def test_fix_status_failed_stops_loop(self, tmp_path):
        entry = _entry()
        catalog = _catalog(entry)

        def capture_fn(catalog, storybook_base_url, output_dir,
                       on_status=None):
            return [_capture(s.story_id, tmp_path=tmp_path)
                    for s in catalog.stories]

        evaluator = MagicMock()
        evaluator.evaluate.return_value = StoryEvalResult(
            story_id=entry.story_id, name=entry.name, title=entry.title,
            overall_score=3,
            issues=[StoryEvalIssue(severity="major", description="x")],
            stop_recommendation="iterate",
        )

        fix_dispatcher = MagicMock()
        fix_dispatcher.dispatch.return_value = StoryFixResult(
            story_id=entry.story_id, name=entry.name, title=entry.title,
            status="failed",
        )

        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=fix_dispatcher,
            discover_fn=lambda root: catalog,
            server_factory=_make_server_factory(),
            capture_fn=capture_fn,
            max_iterations=3,
        )
        result = driver.run(
            frontend_root=Path("/tmp/frontend"),
            screenshots_dir=tmp_path,
        )
        record = result.story_records[0]
        # One eval + one failed fix → stop. No re-eval.
        assert len(record.iterations) == 1
        assert len(record.fixes_applied) == 1
        assert record.final_stop_reason == "fix_failed"

    def test_max_iterations_cap(self, tmp_path):
        entry = _entry()
        catalog = _catalog(entry)

        def capture_fn(catalog, storybook_base_url, output_dir,
                       on_status=None):
            return [_capture(s.story_id, tmp_path=tmp_path)
                    for s in catalog.stories]

        # Always iterate, never reach threshold.
        evaluator = MagicMock()
        evaluator.evaluate.return_value = StoryEvalResult(
            story_id=entry.story_id, name=entry.name, title=entry.title,
            overall_score=3,
            issues=[StoryEvalIssue(severity="major", description="x")],
            stop_recommendation="iterate",
        )

        fix_dispatcher = MagicMock()
        fix_dispatcher.dispatch.return_value = StoryFixResult(
            story_id=entry.story_id, name=entry.name, title=entry.title,
            status="applied",
        )

        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=fix_dispatcher,
            discover_fn=lambda root: catalog,
            server_factory=_make_server_factory(),
            capture_fn=capture_fn,
            max_iterations=2,
        )
        result = driver.run(
            frontend_root=Path("/tmp/frontend"),
            screenshots_dir=tmp_path,
        )
        record = result.story_records[0]
        assert len(record.iterations) == 2
        assert record.final_stop_reason == "max_iterations"


# ── Aggregation ──────────────────────────────────────────────────


class TestAggregation:
    def test_multi_story_score_rolled_up(self, tmp_path):
        a = _entry("a--1", "Default", "A")
        b = _entry("b--1", "Default", "B")
        c = _entry("c--1", "Default", "C")
        catalog = _catalog(a, b, c)

        def capture_fn(catalog, storybook_base_url, output_dir,
                       on_status=None):
            return [_capture(s.story_id, tmp_path=tmp_path)
                    for s in catalog.stories]

        # 9, 5, 8 — mean 7.33; passing 2/3 at threshold 7.
        scores_by_id = {"a--1": 9, "b--1": 5, "c--1": 8}
        evaluator = MagicMock()
        evaluator.evaluate.side_effect = lambda **kw: StoryEvalResult(
            story_id=kw["entry"].story_id,
            name=kw["entry"].name,
            title=kw["entry"].title,
            overall_score=scores_by_id[kw["entry"].story_id],
            stop_recommendation="stop",
        )

        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=MagicMock(),
            discover_fn=lambda root: catalog,
            server_factory=_make_server_factory(),
            capture_fn=capture_fn,
            acceptable_score=7,
        )
        result = driver.run(
            frontend_root=Path("/tmp/frontend"),
            screenshots_dir=tmp_path,
        )
        assert result.score.mean == pytest.approx(7.33, abs=0.01)
        assert result.score.passing == 2
        assert result.score.min == 5
        assert result.score.min_story_id == "b--1"


# ── Design-lock threading ────────────────────────────────────────


class TestDesignLockThreading:
    def test_design_lock_passed_to_evaluator_and_dispatcher(self, tmp_path):
        entry = _entry()
        catalog = _catalog(entry)

        def capture_fn(catalog, storybook_base_url, output_dir,
                       on_status=None):
            return [_capture(s.story_id, tmp_path=tmp_path)
                    for s in catalog.stories]

        ev_calls: list = []
        fix_calls: list = []

        evaluator = MagicMock()
        def fake_eval(**kw):
            ev_calls.append(kw.get("design_lock_json"))
            return StoryEvalResult(
                story_id=entry.story_id, name=entry.name, title=entry.title,
                overall_score=4,
                issues=[StoryEvalIssue(severity="major", description="x")],
                stop_recommendation="iterate",
            )
        evaluator.evaluate.side_effect = fake_eval

        fix_dispatcher = MagicMock()
        def fake_fix(**kw):
            fix_calls.append(kw.get("design_lock_json"))
            return StoryFixResult(
                story_id=entry.story_id, name=entry.name, title=entry.title,
                status="failed",  # short-circuit
            )
        fix_dispatcher.dispatch.side_effect = fake_fix

        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=fix_dispatcher,
            discover_fn=lambda root: catalog,
            server_factory=_make_server_factory(),
            capture_fn=capture_fn,
        )
        lock_json = '{"primary": "#abc"}'
        driver.run(
            frontend_root=Path("/tmp/frontend"),
            screenshots_dir=tmp_path,
            design_lock_json=lock_json,
        )
        assert ev_calls == [lock_json]
        assert fix_calls == [lock_json]

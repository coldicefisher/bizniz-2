"""End-to-end integration test for the Storybook UX loop — Phase 7.

Drives the FULL chain (discover → server-as-mock → capture-as-mock
→ eval-as-mock → fix-as-mock → re-capture → score) through the
real ``StorybookDriver`` against the real Phase 1 discovery output
from the live ``bizniz-skeleton-react`` Toast.stories.tsx fixture.

What's mocked:
- Storybook server (no npm + node required to run this test)
- Playwright capture (would need a real Chromium)
- Vision model call (would need Anthropic API)
- Coder fix dispatch (would need Claude CLI)

What's REAL:
- The skeleton's Toast.stories.tsx file (Phase 1 parses it)
- The actual ``StoryCatalog`` / ``StoryEvalResult`` / ``StoryFixResult``
  / ``StorybookScore`` types and their validators
- ``StorybookDriver``'s control flow: which iters fire, when fix
  dispatches, when scores aggregate

Live validation against an actual running Storybook server is
documented in ``docs/ux_storybook_e2e_runbook.md`` and run
manually when validating a real build.
"""
from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from bizniz.ux_designer.storybook_capture import StoryCaptureResult
from bizniz.ux_designer.storybook_discovery import (
    StoryCatalog, discover_stories,
)
from bizniz.ux_designer.storybook_driver import (
    StorybookDriver, StorybookRunResult,
)
from bizniz.ux_designer.storybook_eval import (
    StoryEvalIssue, StoryEvalResult, StoryEvaluator,
)
from bizniz.ux_designer.storybook_fix import (
    StoryFixDispatcher, StoryFixResult,
)


SKELETON_ROOT = Path.home() / "bizniz-skeleton-react"


def _fake_server_factory():
    """Mock server context that always succeeds. Records start/stop."""
    class _FakeServer:
        def __init__(self, **kw):
            self.started = False
            self.stopped = False
        @property
        def base_url(self):
            return "http://localhost:6006"
        def __enter__(self):
            self.started = True
            return self
        def __exit__(self, exc_type, exc, tb):
            self.stopped = True
    return lambda **kw: _FakeServer(**kw)


@pytest.fixture
def skeleton_catalog() -> StoryCatalog:
    """Real catalog from the React skeleton's Toast.stories.tsx."""
    if not SKELETON_ROOT.is_dir():
        pytest.skip(
            f"skeleton not present at {SKELETON_ROOT} — "
            f"this e2e test requires bizniz-skeleton-react"
        )
    catalog = discover_stories(SKELETON_ROOT)
    if catalog.story_count == 0:
        pytest.skip(
            f"skeleton has no stories — Toast.stories.tsx expected"
        )
    return catalog


class TestEndToEndAgainstSkeleton:
    """Drive the full chain against the real skeleton catalog with
    mocked-but-realistic responses at each external boundary."""

    def test_clean_passing_run(self, skeleton_catalog, tmp_path):
        # Capture: every story succeeds.
        def capture_fn(catalog, storybook_base_url, output_dir,
                       on_status=None):
            return [
                StoryCaptureResult(
                    story_id=s.story_id, name=s.name, title=s.title,
                    screenshot_path=tmp_path / f"{s.story_id}.png",
                    success=True,
                )
                for s in catalog.stories
            ]
        # Eval: clean 9/10 on every story, stop immediately.
        evaluator = MagicMock(spec=StoryEvaluator)
        evaluator.evaluate.side_effect = lambda **kw: StoryEvalResult(
            story_id=kw["entry"].story_id,
            name=kw["entry"].name,
            title=kw["entry"].title,
            overall_score=9,
            matches_design_system=True,
            stop_recommendation="stop",
        )
        fix_dispatcher = MagicMock(spec=StoryFixDispatcher)

        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=fix_dispatcher,
            discover_fn=lambda root: skeleton_catalog,
            server_factory=_fake_server_factory(),
            capture_fn=capture_fn,
            max_iterations=3,
        )
        result = driver.run(
            frontend_root=SKELETON_ROOT,
            screenshots_dir=tmp_path,
        )
        assert result.skipped_reason is None
        assert result.catalog_size == skeleton_catalog.story_count
        assert len(result.story_records) == skeleton_catalog.story_count
        # All stories converged in 1 iter (clean pass).
        for rec in result.story_records:
            assert len(rec.iterations) == 1
            assert rec.final_score == 9
            assert rec.final_stop_reason == "score_met_threshold"
        # Score: every story passing.
        assert result.score.passing == skeleton_catalog.story_count
        assert result.score.failing_story_ids == []
        # Fix never invoked.
        fix_dispatcher.dispatch.assert_not_called()

    def test_one_story_needs_fix_others_clean(
        self, skeleton_catalog, tmp_path,
    ):
        # Convergence pattern: one story needs a fix, others pass.
        # Use the FIRST catalog story as the laggard.
        laggard_id = skeleton_catalog.stories[0].story_id

        def capture_fn(catalog, storybook_base_url, output_dir,
                       on_status=None):
            return [
                StoryCaptureResult(
                    story_id=s.story_id, name=s.name, title=s.title,
                    screenshot_path=tmp_path / f"{s.story_id}.png",
                    success=True,
                )
                for s in catalog.stories
            ]

        # Eval per-story:
        # - Laggard: iter1 = 4/10 iterate, iter2 = 8/10 stop.
        # - Others: 9/10 stop on iter1.
        eval_state = {}
        def fake_eval(*, capture, entry, design_lock_json, iteration):
            if entry.story_id == laggard_id and iteration == 1:
                return StoryEvalResult(
                    story_id=entry.story_id, name=entry.name,
                    title=entry.title, overall_score=4,
                    issues=[StoryEvalIssue(
                        severity="major",
                        description="needs spacing tweak",
                    )],
                    stop_recommendation="iterate",
                )
            if entry.story_id == laggard_id and iteration == 2:
                return StoryEvalResult(
                    story_id=entry.story_id, name=entry.name,
                    title=entry.title, overall_score=8,
                    stop_recommendation="stop",
                )
            return StoryEvalResult(
                story_id=entry.story_id, name=entry.name,
                title=entry.title, overall_score=9,
                stop_recommendation="stop",
            )
        evaluator = MagicMock(spec=StoryEvaluator)
        evaluator.evaluate.side_effect = fake_eval

        # Fix: succeeds for the laggard.
        fix_dispatcher = MagicMock(spec=StoryFixDispatcher)
        fix_dispatcher.dispatch.return_value = StoryFixResult(
            story_id=laggard_id, name="Default", title="Common/Toast",
            status="applied",
            files_written=["/tmp/Toast.tsx"],
        )

        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=fix_dispatcher,
            discover_fn=lambda root: skeleton_catalog,
            server_factory=_fake_server_factory(),
            capture_fn=capture_fn,
            max_iterations=3,
            acceptable_score=7,
        )
        result = driver.run(
            frontend_root=SKELETON_ROOT,
            screenshots_dir=tmp_path,
        )
        # Laggard converged in 2 iters with a fix applied.
        laggard_record = next(
            r for r in result.story_records if r.story_id == laggard_id
        )
        assert len(laggard_record.iterations) == 2
        assert len(laggard_record.fixes_applied) == 1
        assert laggard_record.final_score == 8
        # Score: everyone passing post-fix.
        assert result.score.passing == skeleton_catalog.story_count

    def test_capture_failure_for_one_story_excluded_from_score(
        self, skeleton_catalog, tmp_path,
    ):
        # First story fails capture; remaining stories succeed.
        if skeleton_catalog.story_count < 2:
            pytest.skip("need >= 2 stories to test capture-failure isolation")
        fail_id = skeleton_catalog.stories[0].story_id

        def capture_fn(catalog, storybook_base_url, output_dir,
                       on_status=None):
            out = []
            for s in catalog.stories:
                if s.story_id == fail_id:
                    out.append(StoryCaptureResult(
                        story_id=s.story_id, name=s.name, title=s.title,
                        screenshot_path=None, success=False,
                        error="page load timeout",
                    ))
                else:
                    out.append(StoryCaptureResult(
                        story_id=s.story_id, name=s.name, title=s.title,
                        screenshot_path=tmp_path / f"{s.story_id}.png",
                        success=True,
                    ))
            return out

        evaluator = MagicMock(spec=StoryEvaluator)
        evaluator.evaluate.side_effect = lambda **kw: (
            # Real evaluator handles no-capture itself; for the test
            # we mimic the production behavior by checking capture
            # success.
            StoryEvalResult(
                story_id=kw["entry"].story_id,
                name=kw["entry"].name,
                title=kw["entry"].title,
                overall_score=0,
                summary="no capture available",
                stop_recommendation="stop",
            ) if not kw["capture"].success
            else StoryEvalResult(
                story_id=kw["entry"].story_id,
                name=kw["entry"].name,
                title=kw["entry"].title,
                overall_score=9,
                stop_recommendation="stop",
            )
        )

        driver = StorybookDriver(
            evaluator=evaluator,
            fix_dispatcher=MagicMock(spec=StoryFixDispatcher),
            discover_fn=lambda root: skeleton_catalog,
            server_factory=_fake_server_factory(),
            capture_fn=capture_fn,
        )
        result = driver.run(
            frontend_root=SKELETON_ROOT,
            screenshots_dir=tmp_path,
        )
        # Failed-capture story is not_evaluable; doesn't drag the
        # mean down.
        assert fail_id in result.score.not_evaluable_story_ids
        # Mean equals 9 (only the passing stories contribute).
        assert result.score.mean == 9.0
        assert result.score.covered == skeleton_catalog.story_count - 1


class TestSkeletonCatalogShape:
    """Sanity checks on the real skeleton catalog — surfaces if the
    Toast.stories.tsx contract drifts."""

    def test_at_least_one_story(self, skeleton_catalog):
        assert skeleton_catalog.story_count >= 1

    def test_no_parse_warnings(self, skeleton_catalog):
        # If discovery emits warnings on the canonical Toast story,
        # the parser regex needs widening — surface that loudly.
        assert skeleton_catalog.discovery_warnings == [], (
            f"discovery warnings on skeleton stories: "
            f"{skeleton_catalog.discovery_warnings}"
        )

    def test_component_file_resolved_for_every_story(self, skeleton_catalog):
        # The canonical skeleton stories all have resolvable
        # ``component_file`` paths; if one doesn't, the per-story
        # fix dispatcher loses the file pointer.
        for s in skeleton_catalog.stories:
            assert s.component_file is not None, (
                f"{s.story_id}: component_file not resolved"
            )
            assert s.component_file.is_file(), (
                f"{s.story_id}: component_file {s.component_file} "
                f"does not exist"
            )

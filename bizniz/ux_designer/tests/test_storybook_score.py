"""Tests for ``compute_storybook_score`` — Phase 5 aggregation."""
from __future__ import annotations

import pytest

from bizniz.ux_designer.storybook_eval import StoryEvalResult
from bizniz.ux_designer.storybook_score import compute_storybook_score


def _eval(
    story_id: str, score: int,
    title: str = None, summary: str = "",
) -> StoryEvalResult:
    return StoryEvalResult(
        story_id=story_id,
        name=story_id.split("--")[-1].title(),
        title=title or f"Common/{story_id.split('--')[0].title()}",
        overall_score=score,
        summary=summary,
    )


class TestComputeStorybookScore:
    def test_empty_results(self):
        s = compute_storybook_score([])
        assert s.total == 0
        assert s.mean is None
        assert s.min is None

    def test_all_passing(self):
        s = compute_storybook_score([
            _eval("toast--default", 9),
            _eval("toast--stacked", 8),
            _eval("button--primary", 10),
        ])
        assert s.covered == 3
        assert s.passing == 3
        assert s.failing_story_ids == []
        # Mean ~= 9.0
        assert s.mean == 9.0
        assert s.min == 8
        assert s.min_story_id == "toast--stacked"

    def test_mixed_pass_and_fail_default_threshold(self):
        # Default threshold is 7.
        s = compute_storybook_score([
            _eval("a--1", 9),
            _eval("a--2", 5),  # fail
            _eval("b--1", 7),  # pass (>=)
            _eval("b--2", 3),  # fail
        ])
        assert s.passing == 2
        # Failing sorted ascending by score.
        assert s.failing_story_ids == ["b--2", "a--2"]
        assert s.min == 3
        assert s.min_story_id == "b--2"

    def test_custom_threshold(self):
        s = compute_storybook_score([
            _eval("a--1", 7),
            _eval("a--2", 8),
        ], acceptable_score=8)
        assert s.passing == 1
        assert s.failing_story_ids == ["a--1"]

    def test_not_evaluable_excluded_from_metrics(self):
        # Story with score=0 AND a "no capture" summary is excluded
        # from mean/min calculations.
        s = compute_storybook_score([
            _eval("a--1", 9),
            _eval("a--2", 0, summary="no capture available"),
            _eval("a--3", 8),
        ])
        assert s.covered == 2  # a--2 excluded
        assert s.not_evaluable_story_ids == ["a--2"]
        assert s.total == 3
        # Mean over a--1 + a--3 = 8.5
        assert s.mean == 8.5
        # a--2 is not the laggard — a--3 is.
        assert s.min == 8
        assert s.min_story_id == "a--3"

    def test_real_zero_score_kept_in_metrics(self):
        # A real score of 0 (with no "no capture" summary) IS counted
        # as a failing primitive — distinguish from capture failures.
        s = compute_storybook_score([
            _eval("bad--default", 0, summary="primitive is unstyled"),
            _eval("good--default", 9),
        ])
        assert s.covered == 2
        assert s.passing == 1
        assert s.min == 0
        assert s.min_story_id == "bad--default"
        assert s.not_evaluable_story_ids == []

    def test_all_not_evaluable_returns_no_metrics(self):
        s = compute_storybook_score([
            _eval("a--1", 0, summary="no capture available"),
            _eval("a--2", 0, summary="vision call returned no parseable JSON"),
        ])
        assert s.covered == 0
        assert s.mean is None
        assert s.min is None
        assert len(s.not_evaluable_story_ids) == 2
        assert s.total == 2

    def test_min_story_returned_when_tie(self):
        # Ties on min — first encountered wins (stable per Python's
        # ``min`` semantics).
        s = compute_storybook_score([
            _eval("a--1", 4),
            _eval("a--2", 4),
            _eval("a--3", 9),
        ])
        assert s.min == 4
        # Either a--1 or a--2 is the laggard; the one returned should
        # be deterministic across runs.
        assert s.min_story_id in ("a--1", "a--2")

    def test_title_carried_through(self):
        s = compute_storybook_score([
            _eval("toast--default", 4, title="Common/Toast"),
            _eval("button--primary", 9, title="UI/Button"),
        ])
        assert s.min_title == "Common/Toast"

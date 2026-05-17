"""Tests for the progress-based stopping primitive (D2)."""
from __future__ import annotations

import pytest

from bizniz.lib.progress_tracker import ProgressTracker


class TestVerdicts:
    def test_progress_when_failures_decrease(self):
        t = ProgressTracker(initial_failure_count=5)
        assert t.update(3) == "progress"

    def test_stalled_when_failures_flat(self):
        t = ProgressTracker(initial_failure_count=5)
        assert t.update(5) == "stalled"

    def test_regression_when_failures_increase(self):
        t = ProgressTracker(initial_failure_count=5)
        assert t.update(6) == "regression"

    def test_zero_failures_still_records_progress(self):
        t = ProgressTracker(initial_failure_count=3)
        assert t.update(0) == "progress"
        assert t.has_converged() is True


class TestStallCounter:
    def test_progress_resets_counter(self):
        t = ProgressTracker(initial_failure_count=5, stall_threshold=3)
        t.update(5)  # stalled — counter = 1
        t.update(5)  # stalled — counter = 2
        t.update(3)  # progress — reset to 0
        assert t.consecutive_no_progress == 0

    def test_stalled_increments_counter(self):
        t = ProgressTracker(initial_failure_count=5, stall_threshold=10)
        t.update(5)
        t.update(5)
        t.update(5)
        assert t.consecutive_no_progress == 3

    def test_regression_increments_counter(self):
        t = ProgressTracker(initial_failure_count=5, stall_threshold=10)
        t.update(7)
        t.update(8)
        assert t.consecutive_no_progress == 2

    def test_mixed_stalled_and_regression_both_count(self):
        t = ProgressTracker(initial_failure_count=5, stall_threshold=10)
        t.update(5)   # stalled
        t.update(7)   # regression
        t.update(7)   # stalled
        assert t.consecutive_no_progress == 3


class TestShouldStop:
    def test_does_not_stop_while_progressing(self):
        t = ProgressTracker(initial_failure_count=10, stall_threshold=3)
        for n in (8, 6, 4, 2, 1):
            t.update(n)
            assert t.should_stop() is False, f"Stopped at {n}"

    def test_stops_after_threshold_stalls(self):
        t = ProgressTracker(initial_failure_count=5, stall_threshold=3)
        t.update(5)  # stalled — 1
        t.update(5)  # stalled — 2
        assert t.should_stop() is False
        t.update(5)  # stalled — 3
        assert t.should_stop() is True

    def test_stops_after_threshold_mixed_no_progress(self):
        t = ProgressTracker(initial_failure_count=5, stall_threshold=3)
        t.update(5)   # stalled
        t.update(6)   # regression
        t.update(6)   # stalled
        assert t.should_stop() is True

    def test_recovers_after_run_of_stalls(self):
        # The "chew all night" property: long stretches of stalling
        # don't stop the loop as long as progress eventually lands.
        t = ProgressTracker(initial_failure_count=10, stall_threshold=5)
        # 4 stalls (counter = 4, below threshold)
        for _ in range(4):
            t.update(10)
            assert t.should_stop() is False
        # Progress! Counter resets.
        t.update(8)
        assert t.consecutive_no_progress == 0
        assert t.should_stop() is False
        # 4 stalls again — survives.
        for _ in range(4):
            t.update(8)
            assert t.should_stop() is False


class TestHasConverged:
    def test_false_until_zero(self):
        t = ProgressTracker(initial_failure_count=3)
        t.update(2)
        assert t.has_converged() is False
        t.update(1)
        assert t.has_converged() is False
        t.update(0)
        assert t.has_converged() is True

    def test_initial_zero_means_converged(self):
        t = ProgressTracker(initial_failure_count=0)
        assert t.has_converged() is True


class TestHistory:
    def test_history_records_each_iteration(self):
        t = ProgressTracker(initial_failure_count=5)
        t.update(3)
        t.update(3)
        t.update(1)
        h = t.history
        assert len(h) == 3
        assert h[0].verdict == "progress"
        assert h[0].failure_count_before == 5
        assert h[0].failure_count_after == 3
        assert h[1].verdict == "stalled"
        assert h[2].verdict == "progress"
        assert h[2].iteration_index == 3

    def test_render_includes_arrows_and_counts(self):
        t = ProgressTracker(initial_failure_count=5)
        t.update(3)
        t.update(4)
        rendered = t.render_history()
        assert "↓" in rendered  # progress arrow
        assert "↑" in rendered  # regression arrow
        assert "progress" in rendered
        assert "regression" in rendered


class TestThresholds:
    def test_default_threshold_is_5(self):
        t = ProgressTracker(initial_failure_count=5)
        assert t.stall_threshold == 5
        # 4 stalls = no stop; 5th = stop.
        for _ in range(4):
            t.update(5)
            assert t.should_stop() is False
        t.update(5)
        assert t.should_stop() is True

    def test_custom_threshold(self):
        t = ProgressTracker(initial_failure_count=5, stall_threshold=10)
        for _ in range(9):
            t.update(5)
            assert t.should_stop() is False
        t.update(5)
        assert t.should_stop() is True

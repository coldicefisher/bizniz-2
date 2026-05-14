"""Tests for run_log (item #5)."""
from datetime import datetime
from pathlib import Path

import pytest

from bizniz.ux_designer.run_log import (
    RunSummary,
    append_summary,
    format_trend,
    log_path,
    recent_summaries,
)


def _summary(**over) -> RunSummary:
    defaults = dict(
        timestamp=datetime(2026, 5, 14, 12, 0, 0),
        service="frontend",
        total_s=695.0,
        phase_timings={"capture": 100.0, "eval": 60.0},
        plan_cache_hit=False,
        route_count=11,
        cached_count=10,
        iterated_count=1,
        capture_mismatch_count=1,
        avg_score=7.7,
        final_score_by_route={"/": 8, "/recipes/:id": 3},
        stopped_reasons=["all views iterated"],
    )
    defaults.update(over)
    return RunSummary(**defaults)


class TestAppendAndRead:
    def test_append_then_recent_roundtrip(self, tmp_path):
        append_summary(tmp_path, _summary(total_s=100.0, avg_score=6.5))
        append_summary(tmp_path, _summary(total_s=200.0, avg_score=7.5))
        rows = recent_summaries(tmp_path)
        assert len(rows) == 2
        # Oldest first.
        assert rows[0].total_s == 100.0
        assert rows[1].total_s == 200.0

    def test_recent_returns_last_n(self, tmp_path):
        for i in range(10):
            append_summary(tmp_path, _summary(total_s=float(i)))
        rows = recent_summaries(tmp_path, n=3)
        assert [r.total_s for r in rows] == [7.0, 8.0, 9.0]

    def test_missing_log_returns_empty(self, tmp_path):
        assert recent_summaries(tmp_path) == []

    def test_corrupt_lines_skipped(self, tmp_path):
        fp = log_path(tmp_path)
        fp.parent.mkdir()
        fp.write_text(
            _summary(total_s=42.0).model_dump_json() + "\n"
            + "not json {\n"
            + _summary(total_s=99.0).model_dump_json() + "\n"
        )
        rows = recent_summaries(tmp_path)
        assert [r.total_s for r in rows] == [42.0, 99.0]


class TestFormatTrend:
    def test_empty(self):
        assert "no prior runs" in format_trend([])

    def test_single_run(self):
        s = _summary(total_s=500.0, avg_score=7.5, route_count=10, cached_count=4)
        line = format_trend([s])
        assert "500s" in line
        assert "7.5" in line
        assert "4/10" in line

    def test_multi_run_arrow(self):
        s1 = _summary(total_s=2000.0, avg_score=6.5, route_count=11, cached_count=0)
        s2 = _summary(total_s=700.0, avg_score=7.7, route_count=11, cached_count=10)
        s3 = _summary(total_s=250.0, avg_score=8.0, route_count=11, cached_count=11)
        line = format_trend([s1, s2, s3])
        assert "2000s→700s→250s" in line
        assert "6.5→7.7→8.0" in line
        assert "0/11→10/11→11/11" in line

    def test_handles_none_score(self):
        s = _summary(avg_score=None)
        line = format_trend([s])
        assert "?" in line

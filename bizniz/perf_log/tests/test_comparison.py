"""Tests for the Report → Report → ComparisonReport builder."""
from __future__ import annotations

import pytest

from bizniz.perf_log.aggregators import (
    AgentStats,
    DecomposerStats,
    FailureCounts,
    Report,
    ResumeStats,
    TimingStats,
    UnitStats,
)
from bizniz.perf_log.comparison import (
    ComparisonReport,
    TimingDelta,
    build_comparison,
)


def _t(durations: list[float]) -> TimingStats:
    return TimingStats.from_durations(durations)


def _agent(name: str, durations: list[float], chars: int = 0) -> AgentStats:
    return AgentStats(
        agent=name, timing=_t(durations), response_chars_total=chars,
    )


class TestTimingDelta:
    def test_from_pair_basic(self):
        base = _t([100.0, 200.0, 300.0])
        cand = _t([80.0, 100.0, 200.0])
        d = TimingDelta.from_pair(base, cand)
        assert d.count_delta == 0
        # baseline median 200, candidate median 100 → -100s.
        assert d.median_delta_s == -100.0
        # Improvement = negative pct for "lower is better".
        assert d.median_pct_change == -50.0

    def test_from_pair_zero_baseline_gives_zero_pct(self):
        base = _t([])
        cand = _t([100.0])
        d = TimingDelta.from_pair(base, cand)
        assert d.median_pct_change == 0.0
        assert d.median_delta_s == 100.0


class TestBuildComparison:
    def test_wall_clock_delta(self):
        b = Report(wall_clock_s=1000.0)
        c = Report(wall_clock_s=800.0)
        cmp = build_comparison(b, c)
        assert cmp.wall_clock_delta_s == -200.0
        assert cmp.wall_clock_pct_change == -20.0

    def test_pass_rate_delta(self):
        b = Report(units=UnitStats(
            timing=_t([1.0] * 10), exit_codes={0: 8, 1: 2},
        ))
        c = Report(units=UnitStats(
            timing=_t([1.0] * 10), exit_codes={0: 10},
        ))
        cmp = build_comparison(b, c)
        assert cmp.pass_rate_baseline == 0.8
        assert cmp.pass_rate_candidate == 1.0
        # 0.2 absolute jump in pass rate.
        assert abs(cmp.pass_rate_delta - 0.2) < 1e-9

    def test_decomposer_delta(self):
        b = Report(decomposer=DecomposerStats(
            issues_decomposed=10, units_total=15, expansion_factor=1.5,
            confidence=_t([0.8] * 5), low_confidence_count=2,
        ))
        c = Report(decomposer=DecomposerStats(
            issues_decomposed=10, units_total=25, expansion_factor=2.5,
            confidence=_t([0.85] * 5), low_confidence_count=0,
        ))
        cmp = build_comparison(b, c)
        assert cmp.decomposer_expansion_delta == 1.0
        assert abs(cmp.decomposer_median_confidence_delta - 0.05) < 1e-9
        assert cmp.decomposer_low_confidence_delta == -2

    def test_agent_outer_join(self):
        b = Report(agents=[
            _agent("Decomposer", [10.0, 20.0]),
            _agent("LegacyAgent", [100.0]),  # only in baseline
        ])
        c = Report(agents=[
            _agent("Decomposer", [5.0, 10.0]),
            _agent("NewAgent", [50.0]),  # only in candidate
        ])
        cmp = build_comparison(b, c)
        agents = {ac.agent: ac for ac in cmp.agent_comparisons}
        assert agents["Decomposer"].only_in is None
        assert agents["Decomposer"].timing_delta is not None
        assert agents["LegacyAgent"].only_in == "baseline"
        assert agents["LegacyAgent"].candidate is None
        assert agents["NewAgent"].only_in == "candidate"
        assert agents["NewAgent"].baseline is None

    def test_agent_sort_by_candidate_total_desc(self):
        b = Report(agents=[
            _agent("A", [1.0]),
            _agent("B", [1.0]),
        ])
        c = Report(agents=[
            _agent("A", [10.0]),
            _agent("B", [100.0]),  # bigger
        ])
        cmp = build_comparison(b, c)
        assert cmp.agent_comparisons[0].agent == "B"
        assert cmp.agent_comparisons[1].agent == "A"

    def test_only_baseline_agents_sink_below(self):
        b = Report(agents=[
            _agent("Gone", [1000.0]),  # huge but gone in candidate
        ])
        c = Report(agents=[
            _agent("Tiny", [1.0]),
        ])
        cmp = build_comparison(b, c)
        # Tiny (in candidate) should sort first; Gone (baseline-only)
        # sinks to bottom even though its baseline is huge.
        assert cmp.agent_comparisons[0].agent == "Tiny"
        assert cmp.agent_comparisons[1].agent == "Gone"
        assert cmp.agent_comparisons[1].only_in == "baseline"

    def test_resume_savings_delta(self):
        b = Report(resume=ResumeStats(
            units_skipped_via_resume=10, units_actually_run=90,
        ))  # 10%
        c = Report(resume=ResumeStats(
            units_skipped_via_resume=30, units_actually_run=70,
        ))  # 30%
        cmp = build_comparison(b, c)
        assert abs(cmp.resume_savings_baseline_pct - 10.0) < 1e-9
        assert abs(cmp.resume_savings_candidate_pct - 30.0) < 1e-9
        assert abs(cmp.resume_savings_delta_pct - 20.0) < 1e-9

    def test_failure_deltas(self):
        b = Report(failures=FailureCounts(
            gate_fails=2, gate_halts=1, smoke_recoveries_attempted=3,
            smoke_recoveries_succeeded=2, rate_limits_transient=5,
            rate_limits_usage_cap=1, rate_limit_wait_total_s=600.0,
            readonly_retries=2,
        ))
        c = Report(failures=FailureCounts(
            gate_fails=0, gate_halts=0, smoke_recoveries_attempted=1,
            smoke_recoveries_succeeded=1, rate_limits_transient=1,
            rate_limits_usage_cap=0, rate_limit_wait_total_s=60.0,
            readonly_retries=0,
        ))
        cmp = build_comparison(b, c)
        fd = cmp.failure_deltas
        # All deltas should be negative (improvements).
        assert fd.gate_fails == -2
        assert fd.gate_halts == -1
        assert fd.smoke_recoveries_attempted == -2
        assert fd.smoke_recoveries_succeeded == -1
        assert fd.rate_limits_transient == -4
        assert fd.rate_limits_usage_cap == -1
        assert fd.rate_limit_wait_total_s == -540.0
        assert fd.readonly_retries == -2

    def test_empty_reports_dont_crash(self):
        cmp = build_comparison(Report(), Report())
        assert cmp.wall_clock_delta_s == 0.0
        assert cmp.agent_comparisons == []
        assert cmp.failure_deltas.gate_fails == 0

    def test_reports_preserved_verbatim(self):
        b = Report(source_path="/a.log", event_count=100)
        c = Report(source_path="/b.log", event_count=200)
        cmp = build_comparison(b, c)
        assert cmp.baseline.source_path == "/a.log"
        assert cmp.baseline.event_count == 100
        assert cmp.candidate.source_path == "/b.log"
        assert cmp.candidate.event_count == 200

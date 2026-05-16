"""Tests for the events → Report aggregator."""
from __future__ import annotations

import pytest

from bizniz.perf_log.aggregators import (
    AgentStats,
    Report,
    TimingStats,
    build_report,
)
from bizniz.perf_log.events import (
    AgentCall,
    DecomposerResult,
    GateEvent,
    MilestoneDone,
    ProUXDesignerTiming,
    RateLimitEvent,
    ReadonlyRetryEvent,
    SmokeRecoveryEvent,
    UnitDispatch,
    UnitSkip,
)


# ── TimingStats ──────────────────────────────────────────────────


class TestTimingStats:
    def test_empty(self):
        t = TimingStats.from_durations([])
        assert t.count == 0
        assert t.sum_s == 0.0

    def test_simple(self):
        t = TimingStats.from_durations([10.0, 20.0, 30.0])
        assert t.count == 3
        assert t.sum_s == 60.0
        assert t.mean_s == 20.0
        assert t.min_s == 10.0
        assert t.max_s == 30.0

    def test_unsorted_input_handled(self):
        t = TimingStats.from_durations([30.0, 10.0, 20.0])
        assert t.min_s == 10.0
        assert t.max_s == 30.0


# ── Report aggregation ───────────────────────────────────────────


def _ev(cls, **kw):
    """Construct an event with defaults filled in."""
    return cls(elapsed_s=kw.pop("elapsed_s", 0.0), **kw)


class TestBuildReport:
    def test_empty_events(self):
        r = build_report([])
        assert r.event_count == 0
        assert r.wall_clock_s == 0.0
        assert r.units.timing.count == 0

    def test_wall_clock_from_first_to_last(self):
        events = [
            _ev(AgentCall, agent="X", elapsed_s=0.0, duration_s=10),
            _ev(AgentCall, agent="X", elapsed_s=500.0, duration_s=10),
        ]
        r = build_report(events)
        assert r.wall_clock_s == 500.0

    def test_agent_rollup_sorted_by_total(self):
        events = [
            _ev(AgentCall, agent="Fast", duration_s=10, response_chars=100),
            _ev(AgentCall, agent="Fast", duration_s=20, response_chars=200),
            _ev(AgentCall, agent="Slow", duration_s=100, response_chars=999),
        ]
        r = build_report(events)
        # Slow has bigger total — should sort first.
        assert r.agents[0].agent == "Slow"
        assert r.agents[1].agent == "Fast"
        assert r.agents[1].timing.count == 2
        assert r.agents[1].timing.sum_s == 30
        assert r.agents[1].response_chars_total == 300

    def test_unit_dispatch_rollup(self):
        events = [
            _ev(UnitDispatch, unit_id="BE-001-U1", duration_s=50,
                exit_code=0, parent_issue="BE-001"),
            _ev(UnitDispatch, unit_id="BE-001-U2", duration_s=100,
                exit_code=0, parent_issue="BE-001"),
            _ev(UnitDispatch, unit_id="BE-002-U1", duration_s=200,
                exit_code=1, parent_issue="BE-002"),
        ]
        r = build_report(events)
        assert r.units.timing.count == 3
        assert r.units.timing.sum_s == 350
        assert r.units.exit_codes == {0: 2, 1: 1}
        assert r.units.by_parent_issue_count == {"BE-001": 2, "BE-002": 1}

    def test_decomposer_rollup_with_low_confidence(self):
        events = [
            _ev(DecomposerResult, issue_id="A", unit_count=2, confidence=0.9),
            _ev(DecomposerResult, issue_id="B", unit_count=3, confidence=0.5),  # low
            _ev(DecomposerResult, issue_id="C", unit_count=1, confidence=0.4),  # low
        ]
        r = build_report(events)
        assert r.decomposer.issues_decomposed == 3
        assert r.decomposer.units_total == 6
        assert r.decomposer.expansion_factor == 2.0
        assert r.decomposer.low_confidence_count == 2

    def test_decomposer_no_events_yields_defaults(self):
        r = build_report([_ev(AgentCall, agent="X", duration_s=1)])
        assert r.decomposer.issues_decomposed == 0
        assert r.decomposer.expansion_factor == 1.0

    def test_resume_rollup(self):
        events = [
            _ev(UnitDispatch, unit_id="BE-001-U1", duration_s=50,
                parent_issue="BE-001"),
            _ev(UnitSkip, unit_id="BE-001-U2", service="backend",
                parent_issue="BE-001"),
            _ev(UnitSkip, unit_id="BE-002-U1", service="backend",
                parent_issue="BE-002"),
        ]
        r = build_report(events)
        assert r.resume.units_skipped_via_resume == 2
        assert r.resume.units_actually_run == 1
        assert r.resume.skipped_by_parent_issue == {"BE-001": 1, "BE-002": 1}

    def test_milestone_done_ordered(self):
        events = [
            _ev(MilestoneDone, milestone_index=1, milestone_name="Auth",
                repair_iterations=1, elapsed_s=100),
            _ev(MilestoneDone, milestone_index=2, milestone_name="Contacts",
                repair_iterations=0, elapsed_s=500),
        ]
        r = build_report(events)
        assert len(r.milestones) == 2
        assert r.milestones[0].milestone_index == 1
        assert r.milestones[1].milestone_index == 2

    def test_ux_timing_latest_wins(self):
        events = [
            _ev(ProUXDesignerTiming, total_s=100.0,
                phase_timings={"a": 50.0}),
            _ev(ProUXDesignerTiming, total_s=200.0,
                phase_timings={"b": 100.0}),
        ]
        r = build_report(events)
        # Latest wins (most recent UX-phase observation).
        assert r.ux.total_s == 200.0
        assert "b" in r.ux.phase_timings
        assert "a" not in r.ux.phase_timings

    def test_failure_counts(self):
        events = [
            _ev(GateEvent, gate_name="g1", severity="fail"),
            _ev(GateEvent, gate_name="g2", severity="halt"),
            _ev(SmokeRecoveryEvent, duration_s=10, actions_count=1,
                self_reported_ok=True),
            _ev(SmokeRecoveryEvent, duration_s=20, actions_count=2,
                self_reported_ok=False),
            _ev(RateLimitEvent, detail="usage_cap", wait_s=1800),
            _ev(RateLimitEvent, detail="transient", wait_s=30),
            _ev(ReadonlyRetryEvent),
            _ev(ReadonlyRetryEvent),
        ]
        r = build_report(events)
        f = r.failures
        assert f.gate_fails == 1
        assert f.gate_halts == 1
        assert f.smoke_recoveries_attempted == 2
        assert f.smoke_recoveries_succeeded == 1
        assert f.rate_limits_usage_cap == 1
        assert f.rate_limits_transient == 1
        assert f.rate_limit_wait_total_s == 1830
        assert f.readonly_retries == 2

    def test_source_path_threaded_through(self):
        r = build_report([], source_path="/tmp/test.log")
        assert r.source_path == "/tmp/test.log"

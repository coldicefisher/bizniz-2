"""Aggregators — events → structured Report.

Report schema is the same shape whether we're describing a single
build or comparing two — so the comparison mode (next commit) can
diff two ``Report`` instances mechanically.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from bizniz.perf_log.events import (
    AgentCall,
    DecomposerResult,
    Event,
    GateEvent,
    MilestoneDone,
    ProUXDesignerTiming,
    RateLimitEvent,
    ReadonlyRetryEvent,
    SmokeRecoveryEvent,
    UnitDispatch,
    UnitSkip,
)


# ── Output shapes ────────────────────────────────────────────────


class TimingStats(BaseModel):
    """Summary of a list of durations (seconds)."""
    count: int = 0
    sum_s: float = 0.0
    mean_s: float = 0.0
    median_s: float = 0.0
    p95_s: float = 0.0
    min_s: float = 0.0
    max_s: float = 0.0

    @classmethod
    def from_durations(cls, durations: List[float]) -> "TimingStats":
        if not durations:
            return cls()
        s = sorted(durations)
        n = len(s)
        return cls(
            count=n,
            sum_s=sum(s),
            mean_s=sum(s) / n,
            median_s=s[n // 2] if n else 0.0,
            p95_s=s[min(n - 1, int(n * 0.95))],
            min_s=s[0],
            max_s=s[-1],
        )


class AgentStats(BaseModel):
    """Per-agent rollup."""
    agent: str = ""
    timing: TimingStats = Field(default_factory=TimingStats)
    response_chars_total: int = 0


class UnitStats(BaseModel):
    """Coder unit dispatch rollup."""
    timing: TimingStats = Field(default_factory=TimingStats)
    exit_codes: Dict[int, int] = Field(default_factory=dict)
    by_parent_issue_count: Dict[str, int] = Field(default_factory=dict)


class DecomposerStats(BaseModel):
    """Decomposer rollup."""
    issues_decomposed: int = 0
    units_total: int = 0
    expansion_factor: float = 1.0  # units / issues
    confidence: TimingStats = Field(default_factory=TimingStats)  # reuse stats shape
    low_confidence_count: int = 0  # < 0.6


class MilestoneStats(BaseModel):
    milestone_index: int = 0
    milestone_name: str = ""
    repair_iterations: int = 0


class FailureCounts(BaseModel):
    """Failure modes / bottleneck indicators."""
    gate_fails: int = 0
    gate_halts: int = 0
    smoke_recoveries_attempted: int = 0
    smoke_recoveries_succeeded: int = 0
    rate_limits_transient: int = 0
    rate_limits_usage_cap: int = 0
    rate_limit_wait_total_s: float = 0.0
    readonly_retries: int = 0


class ResumeStats(BaseModel):
    units_skipped_via_resume: int = 0
    units_actually_run: int = 0
    skipped_by_parent_issue: Dict[str, int] = Field(default_factory=dict)


class UXStats(BaseModel):
    """Last-observed ProUXDesigner timing breakdown. (One per
    milestone; the latest wins for the summary number.)"""
    total_s: float = 0.0
    phase_timings: Dict[str, float] = Field(default_factory=dict)


class Report(BaseModel):
    """The full single-build report. Same shape consumed by the
    comparison mode."""
    # Provenance
    source_path: str = ""
    event_count: int = 0
    wall_clock_s: float = 0.0    # last_elapsed - first_elapsed

    # Rollups
    agents: List[AgentStats] = Field(default_factory=list)
    units: UnitStats = Field(default_factory=UnitStats)
    decomposer: DecomposerStats = Field(default_factory=DecomposerStats)
    milestones: List[MilestoneStats] = Field(default_factory=list)
    failures: FailureCounts = Field(default_factory=FailureCounts)
    resume: ResumeStats = Field(default_factory=ResumeStats)
    ux: UXStats = Field(default_factory=UXStats)


# ── Aggregation ──────────────────────────────────────────────────


def build_report(events: List[Event], source_path: str = "") -> Report:
    """Walk a list of events and assemble a Report."""
    report = Report(source_path=source_path, event_count=len(events))
    if not events:
        return report

    report.wall_clock_s = events[-1].elapsed_s - events[0].elapsed_s

    # Per-agent collection.
    agent_durations: Dict[str, List[float]] = defaultdict(list)
    agent_chars: Dict[str, int] = defaultdict(int)

    # Unit dispatch.
    unit_durations: List[float] = []
    exit_codes: Dict[int, int] = defaultdict(int)
    units_by_parent: Dict[str, int] = defaultdict(int)

    # Decomposer.
    decomposer_unit_counts: List[int] = []
    decomposer_confidences: List[float] = []
    decomposer_low_conf = 0
    decomposer_issues = 0

    # Milestones.
    milestones: List[MilestoneStats] = []

    # Failures.
    failures = FailureCounts()

    # Resume.
    resume_skipped = 0
    resume_run = 0
    skipped_by_parent: Dict[str, int] = defaultdict(int)

    # UX timing — last one wins.
    ux = UXStats()

    for ev in events:
        if isinstance(ev, AgentCall):
            agent_durations[ev.agent].append(ev.duration_s)
            agent_chars[ev.agent] += ev.response_chars
        elif isinstance(ev, UnitDispatch):
            unit_durations.append(ev.duration_s)
            exit_codes[ev.exit_code] += 1
            units_by_parent[ev.parent_issue] += 1
            resume_run += 1
        elif isinstance(ev, UnitSkip):
            resume_skipped += 1
            skipped_by_parent[ev.parent_issue] += 1
        elif isinstance(ev, DecomposerResult):
            decomposer_issues += 1
            decomposer_unit_counts.append(ev.unit_count)
            decomposer_confidences.append(ev.confidence)
            if ev.confidence < 0.6:
                decomposer_low_conf += 1
        elif isinstance(ev, MilestoneDone):
            milestones.append(MilestoneStats(
                milestone_index=ev.milestone_index,
                milestone_name=ev.milestone_name,
                repair_iterations=ev.repair_iterations,
            ))
        elif isinstance(ev, ProUXDesignerTiming):
            ux = UXStats(total_s=ev.total_s, phase_timings=dict(ev.phase_timings))
        elif isinstance(ev, GateEvent):
            if ev.severity == "fail":
                failures.gate_fails += 1
            elif ev.severity == "halt":
                failures.gate_halts += 1
        elif isinstance(ev, SmokeRecoveryEvent):
            failures.smoke_recoveries_attempted += 1
            if ev.self_reported_ok:
                failures.smoke_recoveries_succeeded += 1
        elif isinstance(ev, RateLimitEvent):
            if ev.detail == "usage_cap":
                failures.rate_limits_usage_cap += 1
            else:
                failures.rate_limits_transient += 1
            failures.rate_limit_wait_total_s += ev.wait_s
        elif isinstance(ev, ReadonlyRetryEvent):
            failures.readonly_retries += 1

    # Build agent stats.
    for agent, durations in agent_durations.items():
        report.agents.append(AgentStats(
            agent=agent,
            timing=TimingStats.from_durations(durations),
            response_chars_total=agent_chars[agent],
        ))
    # Sort by total time descending for readability.
    report.agents.sort(key=lambda a: -a.timing.sum_s)

    report.units = UnitStats(
        timing=TimingStats.from_durations(unit_durations),
        exit_codes=dict(exit_codes),
        by_parent_issue_count=dict(units_by_parent),
    )

    if decomposer_issues:
        total_units = sum(decomposer_unit_counts)
        report.decomposer = DecomposerStats(
            issues_decomposed=decomposer_issues,
            units_total=total_units,
            expansion_factor=total_units / decomposer_issues,
            confidence=TimingStats.from_durations(decomposer_confidences),
            low_confidence_count=decomposer_low_conf,
        )

    report.milestones = milestones
    report.failures = failures
    report.resume = ResumeStats(
        units_skipped_via_resume=resume_skipped,
        units_actually_run=resume_run,
        skipped_by_parent_issue=dict(skipped_by_parent),
    )
    report.ux = ux

    return report

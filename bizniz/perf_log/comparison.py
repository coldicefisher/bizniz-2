"""Comparison mode — two ``Report`` instances → one ``ComparisonReport``.

Designed for A/B testing the pipeline: compare a baseline build
log to a candidate build log and surface deltas (per-agent timing,
unit dispatch p95, decomposer expansion, pass rate, failure modes).

``ComparisonReport`` is the same kind of structured artifact as
``Report`` — JSON dump is stable, markdown is human-readable, both
can feed downstream tooling once the user moves from "ask Claude"
to "run locally from terminal".
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from bizniz.perf_log.aggregators import (
    AgentStats,
    Report,
    TimingStats,
)


# ── Output shapes ────────────────────────────────────────────────


def _pct_change(baseline: float, candidate: float) -> float:
    """Return percent change. 0 baseline returns 0.0 (no signal)."""
    if baseline == 0:
        return 0.0
    return (candidate - baseline) / baseline * 100.0


class TimingDelta(BaseModel):
    """Side-by-side timing comparison with computed deltas."""
    baseline: TimingStats = Field(default_factory=TimingStats)
    candidate: TimingStats = Field(default_factory=TimingStats)

    count_delta: int = 0
    sum_delta_s: float = 0.0
    median_delta_s: float = 0.0
    p95_delta_s: float = 0.0
    max_delta_s: float = 0.0

    median_pct_change: float = 0.0
    p95_pct_change: float = 0.0
    sum_pct_change: float = 0.0

    @classmethod
    def from_pair(cls, base: TimingStats, cand: TimingStats) -> "TimingDelta":
        return cls(
            baseline=base,
            candidate=cand,
            count_delta=cand.count - base.count,
            sum_delta_s=cand.sum_s - base.sum_s,
            median_delta_s=cand.median_s - base.median_s,
            p95_delta_s=cand.p95_s - base.p95_s,
            max_delta_s=cand.max_s - base.max_s,
            median_pct_change=_pct_change(base.median_s, cand.median_s),
            p95_pct_change=_pct_change(base.p95_s, cand.p95_s),
            sum_pct_change=_pct_change(base.sum_s, cand.sum_s),
        )


class AgentComparison(BaseModel):
    """Per-agent comparison. Either side may be missing if the
    agent only appeared in one of the runs."""
    agent: str
    baseline: Optional[AgentStats] = None
    candidate: Optional[AgentStats] = None
    timing_delta: Optional[TimingDelta] = None
    only_in: Optional[str] = None  # "baseline" / "candidate" / None


class FailureDeltas(BaseModel):
    """Per-field signed delta on failure counts (candidate - baseline)."""
    gate_fails: int = 0
    gate_halts: int = 0
    smoke_recoveries_attempted: int = 0
    smoke_recoveries_succeeded: int = 0
    rate_limits_transient: int = 0
    rate_limits_usage_cap: int = 0
    rate_limit_wait_total_s: float = 0.0
    readonly_retries: int = 0


class ComparisonReport(BaseModel):
    """The full A/B comparison artifact. Contains both Reports
    verbatim plus derived deltas."""
    baseline: Report = Field(default_factory=Report)
    candidate: Report = Field(default_factory=Report)

    wall_clock_delta_s: float = 0.0
    wall_clock_pct_change: float = 0.0

    # Coder unit dispatch.
    units_timing_delta: TimingDelta = Field(default_factory=TimingDelta)
    pass_rate_baseline: float = 0.0      # 0..1
    pass_rate_candidate: float = 0.0
    pass_rate_delta: float = 0.0

    # Decomposer.
    decomposer_expansion_delta: float = 0.0
    decomposer_median_confidence_delta: float = 0.0
    decomposer_low_confidence_delta: int = 0

    # Per-agent.
    agent_comparisons: List[AgentComparison] = Field(default_factory=list)

    # Resume.
    resume_savings_baseline_pct: float = 0.0
    resume_savings_candidate_pct: float = 0.0
    resume_savings_delta_pct: float = 0.0

    # Failures.
    failure_deltas: FailureDeltas = Field(default_factory=FailureDeltas)


# ── Builder ──────────────────────────────────────────────────────


def _pass_rate(exit_codes: Dict[int, int]) -> float:
    total = sum(exit_codes.values())
    if not total:
        return 0.0
    return exit_codes.get(0, 0) / total


def _resume_savings_pct(skipped: int, run: int) -> float:
    total = skipped + run
    if not total:
        return 0.0
    return skipped * 100.0 / total


def build_comparison(baseline: Report, candidate: Report) -> ComparisonReport:
    """Walk both Reports and assemble a ComparisonReport."""
    cmp = ComparisonReport(baseline=baseline, candidate=candidate)

    cmp.wall_clock_delta_s = candidate.wall_clock_s - baseline.wall_clock_s
    cmp.wall_clock_pct_change = _pct_change(
        baseline.wall_clock_s, candidate.wall_clock_s,
    )

    # Unit dispatch.
    cmp.units_timing_delta = TimingDelta.from_pair(
        baseline.units.timing, candidate.units.timing,
    )
    cmp.pass_rate_baseline = _pass_rate(baseline.units.exit_codes)
    cmp.pass_rate_candidate = _pass_rate(candidate.units.exit_codes)
    cmp.pass_rate_delta = cmp.pass_rate_candidate - cmp.pass_rate_baseline

    # Decomposer.
    cmp.decomposer_expansion_delta = (
        candidate.decomposer.expansion_factor
        - baseline.decomposer.expansion_factor
    )
    cmp.decomposer_median_confidence_delta = (
        candidate.decomposer.confidence.median_s
        - baseline.decomposer.confidence.median_s
    )
    cmp.decomposer_low_confidence_delta = (
        candidate.decomposer.low_confidence_count
        - baseline.decomposer.low_confidence_count
    )

    # Per-agent — outer join on agent name.
    base_by_agent = {a.agent: a for a in baseline.agents}
    cand_by_agent = {a.agent: a for a in candidate.agents}
    all_agents = sorted(set(base_by_agent) | set(cand_by_agent))
    for name in all_agents:
        b = base_by_agent.get(name)
        c = cand_by_agent.get(name)
        only_in = None
        if b is None:
            only_in = "candidate"
        elif c is None:
            only_in = "baseline"
        delta = None
        if b is not None and c is not None:
            delta = TimingDelta.from_pair(b.timing, c.timing)
        cmp.agent_comparisons.append(AgentComparison(
            agent=name,
            baseline=b,
            candidate=c,
            timing_delta=delta,
            only_in=only_in,
        ))
    # Sort by candidate total time descending; agents only-in-baseline
    # sink to the bottom (sorted by their own time).
    def _sort_key(ac: AgentComparison) -> float:
        if ac.candidate:
            return -ac.candidate.timing.sum_s
        if ac.baseline:
            return -ac.baseline.timing.sum_s + 1e12  # push below
        return 0.0
    cmp.agent_comparisons.sort(key=_sort_key)

    # Resume.
    cmp.resume_savings_baseline_pct = _resume_savings_pct(
        baseline.resume.units_skipped_via_resume,
        baseline.resume.units_actually_run,
    )
    cmp.resume_savings_candidate_pct = _resume_savings_pct(
        candidate.resume.units_skipped_via_resume,
        candidate.resume.units_actually_run,
    )
    cmp.resume_savings_delta_pct = (
        cmp.resume_savings_candidate_pct - cmp.resume_savings_baseline_pct
    )

    # Failures.
    bf, cf = baseline.failures, candidate.failures
    cmp.failure_deltas = FailureDeltas(
        gate_fails=cf.gate_fails - bf.gate_fails,
        gate_halts=cf.gate_halts - bf.gate_halts,
        smoke_recoveries_attempted=(
            cf.smoke_recoveries_attempted - bf.smoke_recoveries_attempted
        ),
        smoke_recoveries_succeeded=(
            cf.smoke_recoveries_succeeded - bf.smoke_recoveries_succeeded
        ),
        rate_limits_transient=(
            cf.rate_limits_transient - bf.rate_limits_transient
        ),
        rate_limits_usage_cap=(
            cf.rate_limits_usage_cap - bf.rate_limits_usage_cap
        ),
        rate_limit_wait_total_s=(
            cf.rate_limit_wait_total_s - bf.rate_limit_wait_total_s
        ),
        readonly_retries=cf.readonly_retries - bf.readonly_retries,
    )

    return cmp

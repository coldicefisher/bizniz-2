"""Format a Report (or ComparisonReport) as markdown or JSON."""
from __future__ import annotations

import json
from typing import List, Union

from bizniz.perf_log.aggregators import (
    AgentStats,
    Report,
    TimingStats,
)
from bizniz.perf_log.comparison import (
    AgentComparison,
    ComparisonReport,
    TimingDelta,
)


def format_json(
    report: Union[Report, ComparisonReport], indent: int = 2,
) -> str:
    """JSON dump of the full Report or ComparisonReport. Stable
    schema for A/B comparison + downstream tooling."""
    return report.model_dump_json(indent=indent)


def _fmt_s(seconds: float) -> str:
    """``95s → '1m35s'``, ``2700s → '45m'``, sub-minute → '38s'."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m}m"
    if s:
        return f"{m}m{s}s"
    return f"{m}m"


def _timing_row(label: str, t: TimingStats) -> str:
    if t.count == 0:
        return f"| {label} | 0 | — | — | — | — |"
    return (
        f"| {label} | {t.count} | {_fmt_s(t.sum_s)} | "
        f"{_fmt_s(t.median_s)} | {_fmt_s(t.p95_s)} | {_fmt_s(t.max_s)} |"
    )


def format_markdown(report: Report) -> str:
    """Human-readable Markdown report. Designed to land at
    ``<project>/docs/runs/<job_id>/performance.md``."""
    lines: List[str] = []
    p = lines.append

    p("# Build Performance Report")
    p("")
    if report.source_path:
        p(f"**Source log:** `{report.source_path}`  ")
    p(f"**Events parsed:** {report.event_count}  ")
    p(f"**Wall-clock span:** {_fmt_s(report.wall_clock_s)}")
    p("")

    # ── Headline ─────────────────────────────────────────────────
    p("## Headline")
    p("")
    headline_rows = []
    if report.milestones:
        headline_rows.append(
            f"- **Milestones DONE:** {len(report.milestones)} — "
            + ", ".join(f"M{m.milestone_index}" for m in report.milestones)
        )
    if report.units.timing.count or report.resume.units_skipped_via_resume:
        run = report.units.timing.count
        skip = report.resume.units_skipped_via_resume
        total = run + skip
        if total:
            pct = skip * 100 / total
            headline_rows.append(
                f"- **Unit dispatch:** {run} run + {skip} skipped "
                f"via resume ({pct:.0f}% saved)"
            )
    if report.decomposer.issues_decomposed:
        d = report.decomposer
        headline_rows.append(
            f"- **Decomposer:** {d.issues_decomposed} issues → "
            f"{d.units_total} units ({d.expansion_factor:.1f}x), "
            f"median confidence {d.confidence.median_s:.2f}"
        )
    if report.failures.readonly_retries:
        headline_rows.append(
            f"- **Readonly retries:** {report.failures.readonly_retries} "
            f"(the project.db bug — fix is shipped)"
        )
    if report.failures.rate_limit_wait_total_s:
        headline_rows.append(
            f"- **Rate-limit waits:** "
            f"{_fmt_s(report.failures.rate_limit_wait_total_s)} total "
            f"(usage cap: {report.failures.rate_limits_usage_cap}, "
            f"transient: {report.failures.rate_limits_transient})"
        )
    if report.failures.smoke_recoveries_attempted:
        headline_rows.append(
            f"- **Smoke recoveries:** "
            f"{report.failures.smoke_recoveries_succeeded}/"
            f"{report.failures.smoke_recoveries_attempted} succeeded"
        )
    if not headline_rows:
        headline_rows.append("- (no significant events)")
    for row in headline_rows:
        p(row)
    p("")

    # ── Coder unit dispatch ──────────────────────────────────────
    p("## Coder unit dispatch")
    p("")
    p("| | calls | total | median | p95 | max |")
    p("|---|---:|---:|---:|---:|---:|")
    p(_timing_row("All units", report.units.timing))
    p("")
    if report.units.exit_codes:
        bits = ", ".join(
            f"exit {c}: {n}" for c, n in sorted(report.units.exit_codes.items())
        )
        p(f"**Exit codes:** {bits}")
        p("")

    # ── Decomposer ───────────────────────────────────────────────
    if report.decomposer.issues_decomposed:
        d = report.decomposer
        p("## Decomposer")
        p("")
        p(
            f"- {d.issues_decomposed} issues → {d.units_total} units "
            f"({d.expansion_factor:.2f}x average)"
        )
        p(
            f"- Confidence: median={d.confidence.median_s:.2f}, "
            f"min={d.confidence.min_s:.2f}, max={d.confidence.max_s:.2f}"
        )
        if d.low_confidence_count:
            p(
                f"- ⚠️  {d.low_confidence_count} issue(s) decomposed at "
                f"confidence < 0.6 (would trigger re-decompose under "
                f"the AgentConfidence retrofit)"
            )
        p("")

    # ── Resume ───────────────────────────────────────────────────
    if report.resume.units_skipped_via_resume:
        r = report.resume
        p("## Resume savings")
        p("")
        total = r.units_skipped_via_resume + r.units_actually_run
        pct = r.units_skipped_via_resume * 100 / total if total else 0
        p(
            f"- {r.units_skipped_via_resume} of {total} units "
            f"({pct:.0f}%) skipped via issue store — prior-run units "
            f"reused on resume."
        )
        p("")

    # ── Per-agent ────────────────────────────────────────────────
    p("## Per-agent timing")
    p("")
    p("| agent | calls | total | median | p95 | max |")
    p("|---|---:|---:|---:|---:|---:|")
    for a in report.agents:
        p(_timing_row(a.agent, a.timing))
    if not report.agents:
        p("| (no agent calls observed) | | | | | |")
    p("")

    # ── Milestones ───────────────────────────────────────────────
    if report.milestones:
        p("## Milestones DONE")
        p("")
        p("| milestone | name | repair iters |")
        p("|---|---|---:|")
        for m in report.milestones:
            p(f"| M{m.milestone_index} | {m.milestone_name} | {m.repair_iterations} |")
        p("")

    # ── UX timing (last milestone observed) ──────────────────────
    if report.ux.total_s:
        p("## ProUXDesigner (last milestone observed)")
        p("")
        p(f"- Total: **{_fmt_s(report.ux.total_s)}**")
        for phase, dur in sorted(
            report.ux.phase_timings.items(), key=lambda kv: -kv[1],
        ):
            p(f"  - {phase}: {_fmt_s(dur)}")
        p("")

    # ── Failures ─────────────────────────────────────────────────
    f = report.failures
    has_failures = (
        f.gate_fails or f.gate_halts
        or f.smoke_recoveries_attempted or f.rate_limits_transient
        or f.rate_limits_usage_cap or f.readonly_retries
    )
    if has_failures:
        p("## Failure modes / bottleneck signals")
        p("")
        if f.gate_fails or f.gate_halts:
            p(f"- Gates: {f.gate_fails} fail(s), {f.gate_halts} halt(s)")
        if f.smoke_recoveries_attempted:
            p(
                f"- Smoke recoveries: {f.smoke_recoveries_succeeded}/"
                f"{f.smoke_recoveries_attempted} succeeded"
            )
        if f.rate_limits_transient or f.rate_limits_usage_cap:
            p(
                f"- Rate limits: {f.rate_limits_transient} transient, "
                f"{f.rate_limits_usage_cap} usage-cap, "
                f"{_fmt_s(f.rate_limit_wait_total_s)} total wait"
            )
        if f.readonly_retries:
            p(f"- ProjectDB readonly retries: {f.readonly_retries}")
        p("")

    return "\n".join(lines)


# ── Comparison-mode helpers ──────────────────────────────────────


def _fmt_delta_s(seconds: float) -> str:
    """Signed duration, e.g. '+1m23s' or '-37s'."""
    sign = "+" if seconds >= 0 else "-"
    return f"{sign}{_fmt_s(abs(seconds))}"


def _fmt_pct(pct: float) -> str:
    """Signed pct, e.g. '+12%' or '-18%'."""
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


def _verdict(pct_change: float, lower_is_better: bool = True) -> str:
    """Short verdict tag for a metric. faster/slower/flat."""
    if abs(pct_change) < 5:
        return "flat"
    improved = (pct_change < 0) if lower_is_better else (pct_change > 0)
    return "faster" if improved else "slower"


def _timing_compare_rows(label: str, d: TimingDelta) -> List[str]:
    """Return the rows of a per-metric comparison block for one
    side-by-side table."""
    b, c = d.baseline, d.candidate

    def row(metric: str, b_val: str, c_val: str, delta: str, pct: str) -> str:
        return f"| {metric} | {b_val} | {c_val} | {delta} | {pct} |"

    return [
        row(
            f"{label} — calls",
            str(b.count), str(c.count),
            f"{d.count_delta:+d}", "—",
        ),
        row(
            f"{label} — total",
            _fmt_s(b.sum_s), _fmt_s(c.sum_s),
            _fmt_delta_s(d.sum_delta_s), _fmt_pct(d.sum_pct_change),
        ),
        row(
            f"{label} — median",
            _fmt_s(b.median_s), _fmt_s(c.median_s),
            _fmt_delta_s(d.median_delta_s), _fmt_pct(d.median_pct_change),
        ),
        row(
            f"{label} — p95",
            _fmt_s(b.p95_s), _fmt_s(c.p95_s),
            _fmt_delta_s(d.p95_delta_s), _fmt_pct(d.p95_pct_change),
        ),
        row(
            f"{label} — max",
            _fmt_s(b.max_s), _fmt_s(c.max_s),
            _fmt_delta_s(d.max_delta_s), "—",
        ),
    ]


def format_comparison_markdown(cmp: ComparisonReport) -> str:
    """Side-by-side comparison markdown. Output design:

    - Headline at top: wall-clock delta + verdict
    - Coder unit dispatch table (most important metric)
    - Decomposer deltas
    - Per-agent table (only agents with timing delta, sorted by
      candidate impact)
    - Failures: signed delta on each counter
    - Provenance footer with both source paths
    """
    lines: List[str] = []
    p = lines.append

    b, c = cmp.baseline, cmp.candidate

    p("# Build Comparison Report")
    p("")
    p(f"**Baseline:**  `{b.source_path or '(no source)'}` — "
      f"{_fmt_s(b.wall_clock_s)} wall-clock, {b.event_count} events")
    p(f"**Candidate:** `{c.source_path or '(no source)'}` — "
      f"{_fmt_s(c.wall_clock_s)} wall-clock, {c.event_count} events")
    p("")

    # ── Headline ─────────────────────────────────────────────────
    p("## Headline")
    p("")
    wc_verdict = _verdict(cmp.wall_clock_pct_change, lower_is_better=True)
    p(f"- **Wall-clock:** {_fmt_delta_s(cmp.wall_clock_delta_s)} "
      f"({_fmt_pct(cmp.wall_clock_pct_change)}) — {wc_verdict}")

    units = cmp.units_timing_delta
    if units.baseline.count or units.candidate.count:
        v = _verdict(units.median_pct_change, lower_is_better=True)
        p(f"- **Coder unit median:** "
          f"{_fmt_s(units.baseline.median_s)} → {_fmt_s(units.candidate.median_s)} "
          f"({_fmt_pct(units.median_pct_change)}) — {v}")
        p(f"- **Coder unit p95:** "
          f"{_fmt_s(units.baseline.p95_s)} → {_fmt_s(units.candidate.p95_s)} "
          f"({_fmt_pct(units.p95_pct_change)})")
        pass_delta_pct = cmp.pass_rate_delta * 100.0
        p(f"- **Pass rate:** "
          f"{cmp.pass_rate_baseline * 100:.0f}% → "
          f"{cmp.pass_rate_candidate * 100:.0f}% "
          f"({pass_delta_pct:+.0f} pts)")

    if b.decomposer.issues_decomposed or c.decomposer.issues_decomposed:
        p(f"- **Decomposer expansion:** "
          f"{b.decomposer.expansion_factor:.2f}x → "
          f"{c.decomposer.expansion_factor:.2f}x "
          f"({cmp.decomposer_expansion_delta:+.2f})")

    fd = cmp.failure_deltas
    nontrivial_failure = any([
        fd.gate_fails, fd.gate_halts, fd.smoke_recoveries_attempted,
        fd.rate_limits_transient, fd.rate_limits_usage_cap,
        fd.readonly_retries,
    ])
    if nontrivial_failure:
        p(f"- **Failure deltas:** see table below")
    p("")

    # ── Coder unit dispatch (most important metric) ─────────────
    p("## Coder unit dispatch")
    p("")
    p("| metric | baseline | candidate | delta | pct |")
    p("|---|---:|---:|---:|---:|")
    for row in _timing_compare_rows("All units", units):
        p(row)
    if units.baseline.count or units.candidate.count:
        p(
            f"| Pass rate | {cmp.pass_rate_baseline * 100:.0f}% | "
            f"{cmp.pass_rate_candidate * 100:.0f}% | "
            f"{cmp.pass_rate_delta * 100:+.0f} pts | — |"
        )
    p("")

    # ── Decomposer ───────────────────────────────────────────────
    if b.decomposer.issues_decomposed or c.decomposer.issues_decomposed:
        p("## Decomposer")
        p("")
        p("| metric | baseline | candidate | delta |")
        p("|---|---:|---:|---:|")
        p(
            f"| Issues decomposed | {b.decomposer.issues_decomposed} | "
            f"{c.decomposer.issues_decomposed} | "
            f"{c.decomposer.issues_decomposed - b.decomposer.issues_decomposed:+d} |"
        )
        p(
            f"| Units total | {b.decomposer.units_total} | "
            f"{c.decomposer.units_total} | "
            f"{c.decomposer.units_total - b.decomposer.units_total:+d} |"
        )
        p(
            f"| Expansion factor | {b.decomposer.expansion_factor:.2f}x | "
            f"{c.decomposer.expansion_factor:.2f}x | "
            f"{cmp.decomposer_expansion_delta:+.2f} |"
        )
        p(
            f"| Median confidence | {b.decomposer.confidence.median_s:.2f} | "
            f"{c.decomposer.confidence.median_s:.2f} | "
            f"{cmp.decomposer_median_confidence_delta:+.2f} |"
        )
        p(
            f"| Low-confidence (<0.6) | {b.decomposer.low_confidence_count} | "
            f"{c.decomposer.low_confidence_count} | "
            f"{cmp.decomposer_low_confidence_delta:+d} |"
        )
        p("")

    # ── Per-agent ────────────────────────────────────────────────
    p("## Per-agent timing")
    p("")
    p("| agent | baseline median | candidate median | Δ median | pct | total Δ |")
    p("|---|---:|---:|---:|---:|---:|")
    for ac in cmp.agent_comparisons:
        if ac.only_in == "baseline":
            assert ac.baseline is not None
            p(
                f"| {ac.agent} (gone) | {_fmt_s(ac.baseline.timing.median_s)} "
                f"| — | — | — | {_fmt_delta_s(-ac.baseline.timing.sum_s)} |"
            )
        elif ac.only_in == "candidate":
            assert ac.candidate is not None
            p(
                f"| {ac.agent} (new) | — | "
                f"{_fmt_s(ac.candidate.timing.median_s)} | — | — | "
                f"{_fmt_delta_s(ac.candidate.timing.sum_s)} |"
            )
        elif ac.timing_delta is not None:
            d = ac.timing_delta
            p(
                f"| {ac.agent} | {_fmt_s(d.baseline.median_s)} | "
                f"{_fmt_s(d.candidate.median_s)} | "
                f"{_fmt_delta_s(d.median_delta_s)} | "
                f"{_fmt_pct(d.median_pct_change)} | "
                f"{_fmt_delta_s(d.sum_delta_s)} |"
            )
    if not cmp.agent_comparisons:
        p("| (no agent calls in either run) | | | | | |")
    p("")

    # ── Resume ───────────────────────────────────────────────────
    if (
        b.resume.units_skipped_via_resume
        or c.resume.units_skipped_via_resume
    ):
        p("## Resume savings")
        p("")
        p(
            f"- Baseline: {cmp.resume_savings_baseline_pct:.0f}% of units "
            f"skipped via issue store"
        )
        p(
            f"- Candidate: {cmp.resume_savings_candidate_pct:.0f}% of units "
            f"skipped via issue store"
        )
        p(f"- Delta: {cmp.resume_savings_delta_pct:+.0f} pts")
        p("")

    # ── Failures ─────────────────────────────────────────────────
    if nontrivial_failure or any([
        b.failures.gate_fails, b.failures.gate_halts,
        c.failures.gate_fails, c.failures.gate_halts,
    ]):
        p("## Failure modes")
        p("")
        p("| metric | baseline | candidate | delta |")
        p("|---|---:|---:|---:|")
        bf, cf = b.failures, c.failures
        p(f"| Gate fails | {bf.gate_fails} | {cf.gate_fails} | "
          f"{fd.gate_fails:+d} |")
        p(f"| Gate halts | {bf.gate_halts} | {cf.gate_halts} | "
          f"{fd.gate_halts:+d} |")
        p(
            f"| Smoke recoveries (succeeded/attempted) | "
            f"{bf.smoke_recoveries_succeeded}/{bf.smoke_recoveries_attempted} | "
            f"{cf.smoke_recoveries_succeeded}/{cf.smoke_recoveries_attempted} | "
            f"{fd.smoke_recoveries_succeeded:+d}/{fd.smoke_recoveries_attempted:+d} |"
        )
        p(
            f"| Rate limits — transient | {bf.rate_limits_transient} | "
            f"{cf.rate_limits_transient} | {fd.rate_limits_transient:+d} |"
        )
        p(
            f"| Rate limits — usage cap | {bf.rate_limits_usage_cap} | "
            f"{cf.rate_limits_usage_cap} | {fd.rate_limits_usage_cap:+d} |"
        )
        p(
            f"| Rate-limit wait total | "
            f"{_fmt_s(bf.rate_limit_wait_total_s)} | "
            f"{_fmt_s(cf.rate_limit_wait_total_s)} | "
            f"{_fmt_delta_s(fd.rate_limit_wait_total_s)} |"
        )
        p(
            f"| ProjectDB readonly retries | {bf.readonly_retries} | "
            f"{cf.readonly_retries} | {fd.readonly_retries:+d} |"
        )
        p("")

    return "\n".join(lines)

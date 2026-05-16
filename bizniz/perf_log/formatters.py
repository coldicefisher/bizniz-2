"""Format a Report as markdown or JSON."""
from __future__ import annotations

import json
from typing import List

from bizniz.perf_log.aggregators import (
    AgentStats,
    Report,
    TimingStats,
)


def format_json(report: Report, indent: int = 2) -> str:
    """JSON dump of the full Report. Stable schema for A/B
    comparison + downstream tooling."""
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

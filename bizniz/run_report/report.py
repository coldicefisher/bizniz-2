"""Build + render a per-run efficiency report.

The report is generated when ``Architect.build()`` finishes. It pulls
data from:

  - The architecture (services, frameworks, ports, dependencies)
  - The ServiceResult list (issue counts per service, success/failure)
  - The CostTracker (calls, total cost, by-model, by-agent breakdowns)
  - The CostTracker's job timing (start/end timestamps)
  - The BiznizConfig models snapshot (what the agents were configured to use)
  - The previous run's JSON sidecar, if any (for delta-since-last-run)

Two files are emitted to ``<project_root>/docs/runs/``:

  - ``<job_id>.md``    — human-readable
  - ``<job_id>.json``  — machine-readable; powers the delta in future runs
"""
from __future__ import annotations

import datetime
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


_LOG = logging.getLogger(__name__)


# ── Data shape ───────────────────────────────────────────────────────────────


@dataclass
class RunReport:
    """All the inputs render_markdown / serialize need."""

    job_id: str
    project_name: str
    project_slug: str
    project_root: str
    started_at: str          # ISO timestamp
    finished_at: str         # ISO timestamp
    duration_seconds: float
    status: str              # "succeeded" | "failed"

    # Architecture summary — service-level facts only, no nested DTOs.
    services: list[dict] = field(default_factory=list)
    docker_compose_path: Optional[str] = None

    # Engineering outcomes — one row per app service.
    service_results: list[dict] = field(default_factory=list)

    # Models snapshot — what the operator had configured at build time.
    models: dict = field(default_factory=dict)

    # Cost roll-up.
    cost: dict = field(default_factory=dict)


# ── Serialization ───────────────────────────────────────────────────────────


def serialize(report: RunReport) -> dict:
    return asdict(report)


def deserialize(data: dict) -> RunReport:
    return RunReport(**data)


# ── Discovery: previous run for delta ───────────────────────────────────────


def load_previous_run(runs_dir: Path) -> Optional[RunReport]:
    """Return the most recent prior run's data, or None if absent.

    "Most recent" = newest mtime among ``*.json`` files. Skips files
    that don't deserialize cleanly.
    """
    if not runs_dir.is_dir():
        return None
    candidates = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidates:
        try:
            return deserialize(json.loads(p.read_text()))
        except Exception as e:
            _LOG.warning("[run_report] could not load %s (%s) — skipping", p, e)
    return None


# ── Markdown rendering ──────────────────────────────────────────────────────


def render_markdown(
    report: RunReport,
    previous: Optional[RunReport] = None,
) -> str:
    """Render the report as markdown.

    When ``previous`` is provided, includes a "delta since last run"
    section comparing cost, calls, duration, and per-service success.
    """
    out: list[str] = []
    out.append(f"# Run: {report.project_name}")
    out.append("")
    out.append(f"- **job_id**: `{report.job_id}`")
    out.append(f"- **slug**: `{report.project_slug}`")
    out.append(f"- **project_root**: `{report.project_root}`")
    out.append(f"- **status**: **{report.status}**")
    out.append(f"- **started**: {report.started_at}")
    out.append(f"- **finished**: {report.finished_at}")
    out.append(f"- **duration**: {report.duration_seconds:.1f}s "
               f"({_human_duration(report.duration_seconds)})")
    out.append("")

    # ── Architecture ────────────────────────────────────────────────────
    out.append("## Architecture")
    out.append("")
    if report.services:
        out.append("| Service | Type | Framework | Lang | Port | Skeleton | Depends on |")
        out.append("|---|---|---|---|---|---|---|")
        for s in report.services:
            out.append(
                f"| `{s.get('name', '')}` | {s.get('service_type', '')} | "
                f"{s.get('framework', '')} | {s.get('language', '')} | "
                f"{s.get('port', '') or ''} | {s.get('skeleton') or 'none'} | "
                f"{', '.join(s.get('depends_on') or []) or '—'} |"
            )
    else:
        out.append("_(no services)_")
    out.append("")
    if report.docker_compose_path:
        out.append(f"docker-compose: `{report.docker_compose_path}`")
        out.append("")

    # ── Models ──────────────────────────────────────────────────────────
    out.append("## Models")
    out.append("")
    if report.models:
        for k, v in sorted(report.models.items()):
            out.append(f"- `{k}`: {v}")
    else:
        out.append("_(model snapshot unavailable)_")
    out.append("")

    # ── Service results ─────────────────────────────────────────────────
    out.append("## Engineering results")
    out.append("")
    if report.service_results:
        out.append("| Service | Success | Issues passed | Issues total | Error |")
        out.append("|---|---|---|---|---|")
        for r in report.service_results:
            err = r.get("error") or ""
            err = err.replace("|", "\\|")[:80]
            out.append(
                f"| `{r.get('service_name', '')}` | "
                f"{'✓' if r.get('success') else '✗'} | "
                f"{r.get('issues_passed', 0)} | "
                f"{r.get('issues_total', 0)} | "
                f"{err} |"
            )
    else:
        out.append("_(no engineering results — provision-only run)_")
    out.append("")

    # ── Cost ────────────────────────────────────────────────────────────
    out.append("## Cost")
    out.append("")
    cost = report.cost or {}
    out.append(f"- calls: **{cost.get('calls', 0)}**")
    out.append(f"- input tokens: {cost.get('input_tokens', 0):,}")
    out.append(f"- output tokens: {cost.get('output_tokens', 0):,}")
    out.append(f"- total cost: **${cost.get('total_cost', 0.0):.4f}**")
    if cost.get("unpriced_calls"):
        out.append(
            f"- ⚠ {cost['unpriced_calls']} call(s) had no pricing entry "
            f"(models: {sorted(set(cost.get('unpriced_models', [])))})"
        )
    out.append("")
    by_model = cost.get("by_model") or {}
    if by_model:
        out.append("### By model")
        out.append("")
        out.append("| Model | Calls | Input | Output | Cost |")
        out.append("|---|---:|---:|---:|---:|")
        for model, m in sorted(by_model.items()):
            out.append(
                f"| `{model}` | {int(m.get('calls', 0))} | "
                f"{int(m.get('input_tokens', 0)):,} | "
                f"{int(m.get('output_tokens', 0)):,} | "
                f"${m.get('cost', 0.0):.4f} |"
            )
        out.append("")
    by_agent = cost.get("by_agent") or {}
    if by_agent:
        out.append("### By agent")
        out.append("")
        out.append("| Agent | Calls | Cost |")
        out.append("|---|---:|---:|")
        for agent, a in sorted(by_agent.items()):
            out.append(f"| {agent} | {int(a.get('calls', 0))} | ${a.get('cost', 0.0):.4f} |")
        out.append("")

    # ── Delta ───────────────────────────────────────────────────────────
    if previous is not None:
        out.append("## Delta since last run")
        out.append("")
        out.append(f"_(comparing against `{previous.job_id}` — {previous.project_name})_")
        out.append("")
        out.append("| Metric | Previous | This run | Δ |")
        out.append("|---|---:|---:|---:|")
        out.append(_delta_row("Duration (s)", previous.duration_seconds, report.duration_seconds))
        out.append(_delta_row("Calls",
                              (previous.cost or {}).get("calls", 0),
                              (report.cost or {}).get("calls", 0)))
        out.append(_delta_row("Cost ($)",
                              (previous.cost or {}).get("total_cost", 0.0),
                              (report.cost or {}).get("total_cost", 0.0),
                              fmt="${:.4f}"))
        out.append(_delta_row("Input tokens",
                              (previous.cost or {}).get("input_tokens", 0),
                              (report.cost or {}).get("input_tokens", 0)))
        out.append(_delta_row("Output tokens",
                              (previous.cost or {}).get("output_tokens", 0),
                              (report.cost or {}).get("output_tokens", 0)))
        out.append("")

    return "\n".join(out)


# ── Public entrypoint ───────────────────────────────────────────────────────


def write_run_report(
    *,
    project_name: str,
    project_slug: str,
    project_root: Path,
    job_id: str,
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    status: str,
    architecture: Any,            # SystemArchitecture
    service_results: Iterable[Any],  # ServiceResult-like
    cost_summary: Any,            # CostSummary
    models: Optional[dict] = None,
    docker_compose_path: Optional[str] = None,
) -> Path:
    """Build the report, write `<job_id>.md` + `<job_id>.json`.

    Returns the path to the markdown file. Caller wraps in try/except
    so doc generation never crashes the pipeline.
    """
    runs_dir = Path(project_root) / "docs" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    previous = load_previous_run(runs_dir)

    report = RunReport(
        job_id=job_id,
        project_name=project_name,
        project_slug=project_slug,
        project_root=str(project_root),
        started_at=started_at.astimezone(datetime.timezone.utc).isoformat(),
        finished_at=finished_at.astimezone(datetime.timezone.utc).isoformat(),
        duration_seconds=(finished_at - started_at).total_seconds(),
        status=status,
        services=[_summarize_service(s) for s in (architecture.services or [])],
        docker_compose_path=docker_compose_path,
        service_results=[_summarize_result(r) for r in service_results],
        models=dict(models or {}),
        cost=_summarize_cost(cost_summary),
    )

    json_path = runs_dir / f"{job_id}.json"
    md_path = runs_dir / f"{job_id}.md"
    json_path.write_text(json.dumps(serialize(report), indent=2))
    md_path.write_text(render_markdown(report, previous=previous))
    return md_path


# ── Internals ───────────────────────────────────────────────────────────────


def _summarize_service(s: Any) -> dict:
    """Pull the report-relevant fields off a ServiceDefinition without
    requiring the full pydantic schema in tests."""
    g = lambda name, default=None: getattr(s, name, default)
    return {
        "name": g("name", ""),
        "service_type": g("service_type", ""),
        "framework": g("framework", ""),
        "language": g("language", ""),
        "port": g("port"),
        "depends_on": list(g("depends_on", []) or []),
        "skeleton": g("skeleton", "none"),
    }


def _summarize_result(r: Any) -> dict:
    g = lambda name, default=None: getattr(r, name, default)
    return {
        "service_name": g("service_name", ""),
        "workspace_name": g("workspace_name", ""),
        "success": bool(g("success", False)),
        "issues_total": int(g("issues_total", 0) or 0),
        "issues_passed": int(g("issues_passed", 0) or 0),
        "error": g("error"),
    }


def _summarize_cost(summary: Any) -> dict:
    """Convert a CostSummary into a plain dict."""
    g = lambda name, default=None: getattr(summary, name, default)
    return {
        "calls": int(g("calls", 0) or 0),
        "input_tokens": int(g("input_tokens", 0) or 0),
        "output_tokens": int(g("output_tokens", 0) or 0),
        "total_cost": float(g("total_cost", 0.0) or 0.0),
        "by_model": dict(g("by_model", {}) or {}),
        "by_agent": dict(g("by_agent", {}) or {}),
        "unpriced_calls": int(g("unpriced_calls", 0) or 0),
        "unpriced_models": list(g("unpriced_models", []) or []),
    }


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{int(m)}m {int(s)}s"
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{int(h)}h {int(m)}m"


def _delta_row(label: str, prev: float, cur: float, fmt: str = "{:,.0f}") -> str:
    delta = cur - prev
    arrow = "→"
    if delta > 0:
        arrow = "↑"
    elif delta < 0:
        arrow = "↓"
    sign = "+" if delta > 0 else ""
    delta_str = sign + (fmt.format(delta) if abs(delta) >= 0.0001 else "0")
    return f"| {label} | {fmt.format(prev)} | {fmt.format(cur)} | {arrow} {delta_str} |"

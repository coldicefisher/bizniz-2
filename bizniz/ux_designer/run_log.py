"""Per-run summary log for ProUXDesigner (item #5).

Each ``review_frontend`` call appends a single-line JSON summary
to ``<project>/.bizniz/ux_runs.jsonl``. Subsequent runs read the
last few entries and surface trends so the operator can see, at a
glance, whether the loop is converging.

The log is append-only; nothing in the pipeline reads-then-rewrites
it. Tooling can ``tail`` the file or load the last N entries.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


RUN_LOG_FILENAME = "ux_runs.jsonl"


class RunSummary(BaseModel):
    """One row of the run log. Conservative shape: anything ProUXDesigner
    might want to trend across runs goes here as a top-level field."""

    timestamp: datetime = Field(default_factory=datetime.utcnow)
    service: str = ""
    total_s: float = 0.0
    phase_timings: Dict[str, float] = Field(default_factory=dict)
    plan_cache_hit: Optional[bool] = None
    route_count: int = 0
    cached_count: int = 0
    iterated_count: int = 0
    capture_mismatch_count: int = 0
    avg_score: Optional[float] = None
    final_score_by_route: Dict[str, Optional[int]] = Field(default_factory=dict)
    stopped_reasons: List[str] = Field(default_factory=list)


def log_path(workspace_root: Path) -> Path:
    return workspace_root / ".bizniz" / RUN_LOG_FILENAME


def append_summary(workspace_root: Path, summary: RunSummary) -> None:
    """Append one JSON line. Creates the parent dir if missing."""
    fp = log_path(workspace_root)
    fp.parent.mkdir(parents=True, exist_ok=True)
    line = summary.model_dump_json()
    with fp.open("a") as f:
        f.write(line + "\n")


def recent_summaries(
    workspace_root: Path, n: int = 5,
) -> List[RunSummary]:
    """Return the last ``n`` parseable rows, oldest-first. Missing /
    corrupt log returns an empty list."""
    fp = log_path(workspace_root)
    if not fp.exists():
        return []
    try:
        lines = fp.read_text().splitlines()
    except Exception:
        return []
    out: List[RunSummary] = []
    for raw in lines[-n:]:
        if not raw.strip():
            continue
        try:
            out.append(RunSummary.model_validate_json(raw))
        except Exception:
            continue
    return out


def format_trend(summaries: List[RunSummary]) -> str:
    """One-line summary of recent runs for the start-of-run log."""
    if not summaries:
        return "(no prior runs)"
    elapsed = "→".join(f"{s.total_s:.0f}s" for s in summaries)
    scores = []
    for s in summaries:
        scores.append("?" if s.avg_score is None else f"{s.avg_score:.1f}")
    score_trend = "→".join(scores)
    cache_trend = "→".join(
        f"{s.cached_count}/{s.route_count}" for s in summaries
    )
    return (
        f"elapsed {elapsed}, avg score {score_trend}, "
        f"cached {cache_trend}"
    )

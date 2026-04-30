"""Unit tests for bizniz.run_report.report.

Renders synthetic input — no real architect / cost tracker / DB — so
tests stay fast and isolated.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bizniz.run_report.report import (
    RunReport,
    deserialize,
    load_previous_run,
    render_markdown,
    serialize,
    write_run_report,
)


# ── Test helpers ────────────────────────────────────────────────────────────


def _arch():
    """Minimal SystemArchitecture-shape stand-in."""
    return SimpleNamespace(
        project_slug="petgroomer",
        services=[
            SimpleNamespace(
                name="postgres", service_type="database", framework="postgres",
                language="sql", port=5433, depends_on=[], skeleton="none",
            ),
            SimpleNamespace(
                name="api", service_type="backend", framework="fastapi",
                language="python", port=8000, depends_on=["postgres"],
                skeleton="saas-api",
            ),
        ],
    )


def _service_results():
    return [
        SimpleNamespace(
            service_name="api", workspace_name="api", success=True,
            issues_total=4, issues_passed=4, error=None,
        ),
        SimpleNamespace(
            service_name="frontend", workspace_name="frontend", success=False,
            issues_total=3, issues_passed=1, error="some routes failed | 500",
        ),
    ]


def _cost_summary(**overrides):
    """CostSummary-shape stand-in. Override fields per-test."""
    base = SimpleNamespace(
        calls=12,
        input_tokens=15_000,
        output_tokens=4_500,
        total_cost=0.0234,
        by_model={
            "gemini-pro": {"calls": 6, "input_tokens": 10_000,
                           "output_tokens": 2_000, "cost": 0.0180},
            "gemini-flash": {"calls": 6, "input_tokens": 5_000,
                             "output_tokens": 2_500, "cost": 0.0054},
        },
        by_agent={
            "architect": {"calls": 2, "cost": 0.0150},
            "engineer": {"calls": 10, "cost": 0.0084},
        },
        unpriced_calls=0,
        unpriced_models=[],
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ── render_markdown: section presence ───────────────────────────────────────


def test_markdown_includes_all_top_level_sections(tmp_path):
    md_path = write_run_report(
        project_name="Pet Groomer",
        project_slug="petgroomer",
        project_root=tmp_path,
        job_id="job-1",
        started_at=datetime.datetime(2026, 4, 30, 10, 0, tzinfo=datetime.timezone.utc),
        finished_at=datetime.datetime(2026, 4, 30, 10, 5, tzinfo=datetime.timezone.utc),
        status="succeeded",
        architecture=_arch(),
        service_results=_service_results(),
        cost_summary=_cost_summary(),
        models={"architect_model": "gemini-pro"},
        docker_compose_path=str(tmp_path / "compose.yml"),
    )
    md = md_path.read_text()
    for section in ("# Run: Pet Groomer", "## Architecture", "## Models",
                    "## Engineering results", "## Cost", "### By model",
                    "### By agent"):
        assert section in md, f"missing section: {section}"


def test_markdown_includes_service_rows():
    report = RunReport(
        job_id="j", project_name="X", project_slug="x", project_root=".",
        started_at="2026-04-30T10:00:00+00:00",
        finished_at="2026-04-30T10:00:30+00:00",
        duration_seconds=30.0, status="succeeded",
        services=[{"name": "api", "service_type": "backend",
                   "framework": "fastapi", "language": "python",
                   "port": 8000, "depends_on": ["postgres"],
                   "skeleton": "saas-api"}],
        service_results=[{"service_name": "api", "workspace_name": "api",
                          "success": True, "issues_total": 3,
                          "issues_passed": 3, "error": None}],
        models={"architect_model": "gemini-pro"},
        cost={"calls": 1, "input_tokens": 100, "output_tokens": 50,
              "total_cost": 0.001, "by_model": {}, "by_agent": {},
              "unpriced_calls": 0, "unpriced_models": []},
    )
    md = render_markdown(report)
    assert "`api`" in md
    assert "fastapi" in md
    assert "saas-api" in md
    assert "✓" in md  # success indicator


def test_markdown_includes_failed_service_with_error():
    report = RunReport(
        job_id="j", project_name="X", project_slug="x", project_root=".",
        started_at="x", finished_at="x", duration_seconds=0,
        status="failed",
        services=[],
        service_results=[{"service_name": "frontend", "workspace_name": "frontend",
                          "success": False, "issues_total": 2, "issues_passed": 0,
                          "error": "Build failed"}],
        models={},
        cost={"calls": 0, "input_tokens": 0, "output_tokens": 0,
              "total_cost": 0.0, "by_model": {}, "by_agent": {},
              "unpriced_calls": 0, "unpriced_models": []},
    )
    md = render_markdown(report)
    assert "✗" in md
    assert "Build failed" in md


# ── delta-since-last-run ────────────────────────────────────────────────────


def test_delta_section_renders_when_previous_present(tmp_path):
    """The delta section appears with arrows and computed differences."""
    runs_dir = tmp_path / "docs" / "runs"
    runs_dir.mkdir(parents=True)

    # Prior run JSON sidecar.
    prev = RunReport(
        job_id="prev", project_name="P", project_slug="p", project_root=".",
        started_at="x", finished_at="x", duration_seconds=120.0,
        status="succeeded",
        services=[], service_results=[], models={},
        cost={"calls": 10, "input_tokens": 1_000, "output_tokens": 500,
              "total_cost": 0.0500, "by_model": {}, "by_agent": {},
              "unpriced_calls": 0, "unpriced_models": []},
    )
    (runs_dir / "prev.json").write_text(json.dumps(serialize(prev)))

    # Now write a new run that's faster + cheaper.
    md_path = write_run_report(
        project_name="P", project_slug="p", project_root=tmp_path,
        job_id="cur",
        started_at=datetime.datetime(2026, 4, 30, 12, 0, tzinfo=datetime.timezone.utc),
        finished_at=datetime.datetime(2026, 4, 30, 12, 1, 30, tzinfo=datetime.timezone.utc),
        status="succeeded",
        architecture=_arch(),
        service_results=[],
        cost_summary=_cost_summary(calls=8, input_tokens=900,
                                    output_tokens=400, total_cost=0.0400),
        models={},
    )
    md = md_path.read_text()
    assert "## Delta since last run" in md
    assert "prev" in md
    # Cost went down — should show a ↓ arrow.
    assert "↓" in md


def test_no_delta_section_when_no_previous(tmp_path):
    md_path = write_run_report(
        project_name="P", project_slug="p", project_root=tmp_path,
        job_id="first",
        started_at=datetime.datetime(2026, 4, 30, 12, 0, tzinfo=datetime.timezone.utc),
        finished_at=datetime.datetime(2026, 4, 30, 12, 1, tzinfo=datetime.timezone.utc),
        status="succeeded",
        architecture=_arch(),
        service_results=[],
        cost_summary=_cost_summary(),
        models={},
    )
    md = md_path.read_text()
    assert "## Delta since last run" not in md


# ── load_previous_run ───────────────────────────────────────────────────────


def test_load_previous_run_picks_newest_by_mtime(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    older = RunReport(job_id="old", project_name="O", project_slug="o",
                      project_root=".", started_at="x", finished_at="x",
                      duration_seconds=0, status="ok")
    newer = RunReport(job_id="new", project_name="N", project_slug="n",
                      project_root=".", started_at="x", finished_at="x",
                      duration_seconds=0, status="ok")
    (runs_dir / "old.json").write_text(json.dumps(serialize(older)))
    import time as _t
    _t.sleep(0.01)  # ensure mtime ordering is deterministic
    (runs_dir / "new.json").write_text(json.dumps(serialize(newer)))

    got = load_previous_run(runs_dir)
    assert got is not None
    assert got.job_id == "new"


def test_load_previous_run_returns_none_for_missing_dir(tmp_path):
    assert load_previous_run(tmp_path / "doesnt_exist") is None


def test_load_previous_run_skips_corrupt_files(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "bad.json").write_text("{ not json")
    good = RunReport(job_id="good", project_name="G", project_slug="g",
                     project_root=".", started_at="x", finished_at="x",
                     duration_seconds=0, status="ok")
    (runs_dir / "good.json").write_text(json.dumps(serialize(good)))
    got = load_previous_run(runs_dir)
    assert got is not None
    assert got.job_id == "good"


# ── write_run_report sidecar shape ──────────────────────────────────────────


def test_write_run_report_emits_both_md_and_json(tmp_path):
    md_path = write_run_report(
        project_name="X", project_slug="x", project_root=tmp_path,
        job_id="abc",
        started_at=datetime.datetime(2026, 4, 30, 12, 0, tzinfo=datetime.timezone.utc),
        finished_at=datetime.datetime(2026, 4, 30, 12, 0, 5, tzinfo=datetime.timezone.utc),
        status="succeeded",
        architecture=_arch(),
        service_results=[],
        cost_summary=_cost_summary(),
        models={},
    )
    json_path = md_path.with_suffix(".json")
    assert md_path.is_file()
    assert json_path.is_file()
    parsed = json.loads(json_path.read_text())
    assert parsed["job_id"] == "abc"
    assert parsed["status"] == "succeeded"
    assert parsed["cost"]["calls"] == 12


def test_write_run_report_creates_runs_dir_when_missing(tmp_path):
    """No docs/runs/ yet — function should create it."""
    write_run_report(
        project_name="X", project_slug="x", project_root=tmp_path,
        job_id="j",
        started_at=datetime.datetime(2026, 4, 30, 12, 0, tzinfo=datetime.timezone.utc),
        finished_at=datetime.datetime(2026, 4, 30, 12, 0, 5, tzinfo=datetime.timezone.utc),
        status="succeeded",
        architecture=_arch(),
        service_results=[],
        cost_summary=_cost_summary(),
    )
    assert (tmp_path / "docs" / "runs").is_dir()


def test_serialize_round_trips():
    """RunReport <-> dict <-> RunReport preserves fields."""
    r = RunReport(
        job_id="j", project_name="X", project_slug="x",
        project_root="/tmp/x",
        started_at="2026-04-30T12:00:00+00:00",
        finished_at="2026-04-30T12:00:30+00:00",
        duration_seconds=30.0, status="succeeded",
        services=[{"name": "api"}],
        service_results=[{"service_name": "api", "success": True}],
        models={"architect_model": "gemini-pro"},
        cost={"calls": 5, "total_cost": 0.01, "by_model": {}, "by_agent": {},
              "input_tokens": 100, "output_tokens": 50,
              "unpriced_calls": 0, "unpriced_models": []},
    )
    parsed = deserialize(json.loads(json.dumps(serialize(r))))
    assert parsed.job_id == r.job_id
    assert parsed.cost["calls"] == 5
    assert parsed.services == [{"name": "api"}]

"""Per-run efficiency reports.

After ``Architect.build()`` finishes, ``write_run_report(...)`` lays
down two files under ``<project_root>/docs/runs/``:

  - ``<job_id>.md``    — human-readable summary
  - ``<job_id>.json``  — machine-readable counterpart powering the
                         "delta since last run" section of future runs

Reports include: architecture summary, models config snapshot, per-
service results, cost roll-up, wall-clock time, and (when a previous
run exists) a delta block.
"""
from bizniz.run_report.report import (
    RunReport,
    write_run_report,
    render_markdown,
    load_previous_run,
)

__all__ = [
    "RunReport",
    "write_run_report",
    "render_markdown",
    "load_previous_run",
]

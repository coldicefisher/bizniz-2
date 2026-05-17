"""``python -m bizniz.cleanup`` — operator CLI for ephemeral hygiene.

Three knobs:

- ``--exec`` — prune stale docker test exec dirs
  (``$XDG_RUNTIME_DIR/bizniz/exec/``).
- ``--logs`` — prune stale v2_build log files
  (``$XDG_RUNTIME_DIR/bizniz/logs/``).
- ``--runs`` — prune old ``.bizniz/runs/<job_id>/`` directories under
  ``~/bizniz_projects/<slug>/``, keeping the newest N per project.

Persistent project state (``~/bizniz_projects/<slug>/`` source trees,
the most-recent N runs per project, the cost ledger) is NEVER
touched by this tool.

Examples::

    # Clean everything older than 24h
    python -m bizniz.cleanup --all

    # Only exec dirs older than 1h (between back-to-back runs)
    python -m bizniz.cleanup --exec --max-age-hours 1

    # Old runs for one project, keep last 3
    python -m bizniz.cleanup --runs --project recipe_v2 --keep 3

    # Dry-run — show what would be removed without doing it
    python -m bizniz.cleanup --all --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterable, List, Tuple

from bizniz.lib import ephemeral


def _format_summary(label: str, removed: int, failed: int) -> str:
    line = f"  {label}: {removed} removed"
    if failed:
        line += f", {failed} failed"
    return line


def _projects_root() -> Path:
    import os
    return Path(os.environ.get("BIZNIZ_PROJECTS_ROOT") or
                str(Path.home() / "bizniz_projects"))


def _list_project_runs(project_dir: Path) -> List[Path]:
    """Return ``<project>/.bizniz/runs/<job_id>/`` directories sorted
    oldest → newest by mtime. Empty list if the path doesn't exist."""
    runs_root = project_dir / ".bizniz" / "runs"
    if not runs_root.exists():
        return []
    entries: List[Tuple[float, Path]] = []
    for entry in runs_root.iterdir():
        if not entry.is_dir():
            continue
        try:
            entries.append((entry.stat().st_mtime, entry))
        except OSError:
            continue
    entries.sort()
    return [p for _, p in entries]


def _prune_runs(
    project: str,
    keep: int,
    dry_run: bool,
) -> Tuple[int, int]:
    """Remove all but the newest ``keep`` runs for one project.
    Returns ``(removed, failed)``. ``keep=0`` removes them all."""
    project_dir = _projects_root() / project
    runs = _list_project_runs(project_dir)
    if len(runs) <= keep:
        return (0, 0)
    victims = runs[: len(runs) - keep]
    removed = 0
    failed = 0
    for v in victims:
        if dry_run:
            print(f"  [dry-run] would remove {v}")
            removed += 1
            continue
        if ephemeral.remove_path(v):
            removed += 1
        else:
            failed += 1
            print(f"  failed: {v}", file=sys.stderr)
    return (removed, failed)


def _all_project_slugs() -> Iterable[str]:
    root = _projects_root()
    if not root.exists():
        return []
    return [p.name for p in root.iterdir() if p.is_dir()]


def _cleanup_ephemeral(
    *,
    include_exec: bool,
    include_logs: bool,
    max_age_hours: float,
    dry_run: bool,
) -> dict:
    """Wraps ``ephemeral.cleanup_stale`` with dry-run support."""
    if not dry_run:
        return ephemeral.cleanup_stale(
            max_age_hours=max_age_hours,
            include_exec=include_exec,
            include_logs=include_logs,
        )
    # Dry-run path — count what would be removed without deleting.
    summary = {"exec_removed": 0, "exec_failed": 0,
               "logs_removed": 0, "logs_failed": 0}
    if include_exec:
        for entry in ephemeral.iter_stale(
            ephemeral.get_exec_root(), max_age_hours,
        ):
            print(f"  [dry-run] would remove {entry}")
            summary["exec_removed"] += 1
    if include_logs:
        for entry in ephemeral.iter_stale(
            ephemeral.get_log_dir(), max_age_hours,
        ):
            print(f"  [dry-run] would remove {entry}")
            summary["logs_removed"] += 1
    return summary


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bizniz.cleanup",
        description=__doc__.split("\n\n", 1)[0],
    )
    parser.add_argument("--exec", action="store_true",
                        help="Prune stale docker exec dirs")
    parser.add_argument("--logs", action="store_true",
                        help="Prune stale v2_build log files")
    parser.add_argument("--runs", action="store_true",
                        help="Prune old .bizniz/runs/<job_id> per project")
    parser.add_argument("--all", action="store_true",
                        help="Same as --exec --logs --runs")
    parser.add_argument("--project", default=None,
                        help="Limit --runs to one project slug "
                             "(omit to clean every project under "
                             "~/bizniz_projects/)")
    parser.add_argument("--keep", type=int, default=3,
                        help="Number of most-recent runs to keep per "
                             "project (default 3)")
    parser.add_argument("--max-age-hours", type=float, default=24.0,
                        help="Entries older than this are considered "
                             "stale for --exec and --logs (default 24)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be removed without "
                             "actually deleting")

    args = parser.parse_args(argv)

    if args.all:
        args.exec = args.logs = args.runs = True
    if not (args.exec or args.logs or args.runs):
        parser.error("at least one of --exec/--logs/--runs/--all required")

    print(f"bizniz.cleanup (dry-run={args.dry_run})")
    print(f"  ephemeral root: {ephemeral.get_ephemeral_root()}")

    if args.exec or args.logs:
        summary = _cleanup_ephemeral(
            include_exec=args.exec,
            include_logs=args.logs,
            max_age_hours=args.max_age_hours,
            dry_run=args.dry_run,
        )
        if args.exec:
            print(_format_summary("exec", summary["exec_removed"],
                                  summary["exec_failed"]))
        if args.logs:
            print(_format_summary("logs", summary["logs_removed"],
                                  summary["logs_failed"]))

    if args.runs:
        targets = (
            [args.project] if args.project else list(_all_project_slugs())
        )
        if not targets:
            print("  runs: no projects found under ~/bizniz_projects/")
        for slug in targets:
            removed, failed = _prune_runs(
                slug, keep=args.keep, dry_run=args.dry_run,
            )
            print(_format_summary(f"runs[{slug}]", removed, failed))

    return 0


if __name__ == "__main__":
    sys.exit(main())

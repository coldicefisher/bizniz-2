"""ux_report — trend across past UX review runs for a project.

Reads ``<project>/.bizniz/ux_runs.jsonl`` and prints:

  - elapsed-time trend (last N runs)
  - app-score trend (mean across routes per run)
  - per-route score history (each route's score over the last N runs)
  - convergence story (capture-mismatch + iter counts per route)

Usage:
    PYTHONPATH=. .venv/bin/python -u examples/ux_report.py \\
      ~/bizniz_projects/recipe_box

    # Or limit history depth (default 10):
    PYTHONPATH=. .venv/bin/python -u examples/ux_report.py \\
      ~/bizniz_projects/recipe_box --last 5
"""
import argparse
import sys
from pathlib import Path

from bizniz.ux_designer.run_log import recent_summaries


def main() -> None:
    p = argparse.ArgumentParser(description="UX review trend report")
    p.add_argument("project_root", type=Path, help="Built project root")
    p.add_argument("--last", type=int, default=10,
                   help="How many recent runs to include (default 10)")
    args = p.parse_args()

    project_root = args.project_root.expanduser().resolve()
    if not project_root.is_dir():
        sys.exit(f"ERROR: {project_root} is not a directory")

    # ProUXDesigner persists per-service (workspace.root is the
    # frontend service dir, not the project root). Walk frontends
    # first; fall back to the project root for legacy projects that
    # log there.
    candidate_roots = [
        project_root / "frontend",
        project_root / "ui",
        project_root / "web",
        project_root,
    ]
    rows = []
    used_root = None
    for r in candidate_roots:
        if not r.is_dir():
            continue
        rows = recent_summaries(r, n=args.last)
        if rows:
            used_root = r
            break

    if not rows:
        print(f"\nNo UX runs logged for {project_root.name}.\n")
        print(f"Expected (checked in order):")
        for r in candidate_roots:
            print(f"  - {r}/.bizniz/ux_runs.jsonl")
        return
    if used_root != project_root:
        print(f"  (reading from {used_root.relative_to(project_root)}/.bizniz/)")

    print(f"\n{'='*70}")
    print(f"  UX Review Trend — {project_root.name}")
    print(f"  {len(rows)} run(s)")
    print(f"{'='*70}\n")

    # Elapsed + mean score per run
    print(f"  {'#':>3}  {'when':<20} {'elapsed':>8} {'avg':>5} "
          f"{'pass':>6} {'cached':>7}  notes")
    for i, r in enumerate(rows, 1):
        when = r.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        elapsed = f"{r.total_s:.0f}s"
        avg = "  ?" if r.avg_score is None else f"{r.avg_score:.1f}"
        passing = f"{r.cached_count + r.iterated_count - len(r.stopped_reasons or []) or '?'}"
        cached = f"{r.cached_count}/{r.route_count}"
        notes = []
        if r.plan_cache_hit:
            notes.append("plan-cache HIT")
        if r.capture_mismatch_count:
            notes.append(f"{r.capture_mismatch_count} capture mismatch")
        notes_s = " · ".join(notes) if notes else ""
        print(f"  {i:>3}  {when:<20} {elapsed:>8} {avg:>5} "
              f"{passing:>6} {cached:>7}  {notes_s}")

    # Per-route trend
    print(f"\n{'─'*70}")
    print(f"  Per-route score history (most recent → oldest left)")
    print(f"{'─'*70}\n")
    all_routes = sorted({
        r for s in rows for r in (s.final_score_by_route or {})
    })
    if all_routes:
        cols = list(reversed(rows))  # newest first
        header = "  " + "route".ljust(28) + "".join(
            f"  R{len(rows)-i}" for i in range(len(rows))
        )
        print(header)
        for route in all_routes:
            cells = []
            for s in cols:
                v = (s.final_score_by_route or {}).get(route)
                cells.append("  --" if v is None else f"  {v:>2}")
            print(f"  {route:<28}" + "".join(cells))
    else:
        print("  (no per-route data in run log)")

    # Latest run drill-down
    last = rows[-1]
    print(f"\n{'─'*70}")
    print(f"  Latest run — phase timings")
    print(f"{'─'*70}\n")
    for phase, t in sorted(last.phase_timings.items(), key=lambda kv: -kv[1]):
        print(f"  {phase:<24} {t:>8.1f}s")
    print()


if __name__ == "__main__":
    main()

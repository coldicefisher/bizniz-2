"""v2-audit — reconcile our cost-tracker estimates against Google's actual billing.

Two modes:

  v2-audit --project pet_groomer
      → summary of our tracker spend across all runs (no comparison)

  v2-audit --project pet_groomer --csv ~/Downloads/google_billing.csv
      → comparison: tracker vs Google. Diff per day, per model, grand
        total. Output written to <runs>/audit.md AND printed.

  v2-audit --project pet_groomer --csv X.csv --since 2026-05-01 --until 2026-05-07
      → bounded period.

The tracker side reads ``<project>/docs/runs/*/costs.md`` files
(parsing the per-call breakdown lines our CLI writes). No DB access,
no auth, no API keys. Pull the CSV from Google Cloud Console →
Billing → Reports → Export. Any Detailed daily costs CSV works; we
auto-detect the date / SKU / cost columns.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

# Repo root on PYTHONPATH for direct script invocation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bizniz.cost.audit import (
    compare,
    parse_google_billing_csv,
    parse_project_runs,
    render_diff,
)
from bizniz.cost.ledger import CostLedger, get_default_ledger_path


def _resolve_project_root(slug: str) -> Path:
    base = Path(os.environ.get("BIZNIZ_PROJECTS_ROOT") or
                str(Path.home() / "bizniz_projects"))
    return base / slug


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    p = argparse.ArgumentParser(prog="v2-audit", description="Cost audit")
    p.add_argument("--project", default=None,
                   help="Project slug (omit to read the cumulative ledger)")
    p.add_argument("--ledger", action="store_true",
                   help="Use the cumulative ledger (~/.bizniz/cost_ledger.jsonl) "
                        "instead of project-scoped costs.md. Survives "
                        "project-dir wipes. Auto-implied when --project is omitted.")
    p.add_argument("--csv", default=None, help="Path to Google billing CSV")
    p.add_argument("--since", default=None, type=_parse_date,
                   help="Period start (YYYY-MM-DD), inclusive")
    p.add_argument("--until", default=None, type=_parse_date,
                   help="Period end (YYYY-MM-DD), inclusive")
    p.add_argument("--out", default=None,
                   help="Write audit markdown to this path. Defaults to "
                        "<project>/docs/runs/audit.md or ~/.bizniz/audit.md")
    args = p.parse_args()

    use_ledger = args.ledger or args.project is None
    if use_ledger:
        ledger = CostLedger()
        ledger_entries = ledger.read_all(
            since=args.since, until=args.until,
            project_slug=args.project,  # may be None → all projects
        )
        # Adapt LedgerEntry → TrackerEntry shape for compare().
        from bizniz.cost.audit import TrackerEntry
        tracker_entries = [
            TrackerEntry(
                timestamp=e.ts,
                agent=e.agent,
                model=e.model,
                input_tokens=e.input_tokens,
                output_tokens=e.output_tokens,
                cached_input_tokens=e.cached_input_tokens,
                cost=e.cost,
                phase=e.phase or "",
            )
            for e in ledger_entries
        ]
        print(
            f"Tracker: {len(tracker_entries)} ledger entries "
            f"({'all projects' if not args.project else args.project}) "
            f"from {ledger.path}",
            file=sys.stderr,
        )
        if not args.out:
            args.out = str(get_default_ledger_path().parent / "audit.md")
        out_label = "ledger"
    else:
        project_root = _resolve_project_root(args.project)
        if not project_root.exists():
            p.error(f"project root not found: {project_root}")
        tracker_entries = parse_project_runs(project_root)
        print(f"Tracker: {len(tracker_entries)} per-call records "
              f"across {project_root}/docs/runs/", file=sys.stderr)
        out_label = "project"

    google_entries = []
    if args.csv:
        google_entries = parse_google_billing_csv(Path(args.csv))
        print(f"Google: {len(google_entries)} billing rows from {args.csv}",
              file=sys.stderr)

    diff = compare(
        tracker_entries, google_entries,
        start=args.since, end=args.until,
    )
    report = render_diff(diff)

    out_path = Path(args.out) if args.out else (
        project_root / "docs" / "runs" / "audit.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(report)
    print("", file=sys.stderr)
    print(f"Audit report written to {out_path} (source: {out_label})", file=sys.stderr)


if __name__ == "__main__":
    main()

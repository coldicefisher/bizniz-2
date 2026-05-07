"""v2-backfill-ledger — one-time migration of historical cost data
into the cumulative ledger.

Walks ``~/bizniz_projects/`` and:
  - For each project's ``.bizniz/project.db``, copies api_calls rows
    (v1 era) into the ledger.
  - For each project's ``docs/runs/*/costs.md``, copies per-call lines
    (v2 era) into the ledger.

Idempotency warning: this is NOT idempotent. Running twice doubles
ledger entries for every backfilled call. Recommended workflow:

  1. Move/delete the existing ledger (``~/.bizniz/cost_ledger.jsonl``)
  2. Run this script once
  3. Confirm the ledger total matches your expectation
  4. Resume normal use

The script prints a per-project summary and the cumulative count.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bizniz.cost.ledger import (
    CostLedger,
    backfill_from_costs_md,
    backfill_from_sqlite,
    get_default_ledger_path,
)


def _resolve_projects_root() -> Path:
    return Path(
        os.environ.get("BIZNIZ_PROJECTS_ROOT")
        or str(Path.home() / "bizniz_projects")
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="v2-backfill-ledger")
    p.add_argument("--ledger", default=None,
                   help="Override ledger path (defaults to BIZNIZ_COST_LEDGER "
                        "env or ~/.bizniz/cost_ledger.jsonl)")
    p.add_argument("--dry-run", action="store_true",
                   help="Count what would be backfilled without writing")
    p.add_argument("--projects-root", default=None,
                   help="Override ~/bizniz_projects/")
    args = p.parse_args()

    ledger_path = Path(args.ledger).expanduser() if args.ledger else get_default_ledger_path()
    if ledger_path.exists() and not args.dry_run:
        print(f"WARN: ledger exists at {ledger_path}", file=sys.stderr)
        print(f"      Backfill will APPEND, not replace. Move/delete it first if",
              file=sys.stderr)
        print(f"      you want a clean migration.", file=sys.stderr)
        ans = input("Continue? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.", file=sys.stderr)
            sys.exit(1)

    ledger = CostLedger(path=ledger_path) if not args.dry_run else None
    projects_root = Path(args.projects_root) if args.projects_root else _resolve_projects_root()
    if not projects_root.exists():
        print(f"projects root not found: {projects_root}", file=sys.stderr)
        sys.exit(1)

    grand_db = 0
    grand_md = 0
    print(f"{'project':<35s} {'sqlite':>8s} {'costs.md':>10s}")
    print(f"{'-' * 35:<35s} {'-' * 8:>8s} {'-' * 10:>10s}")

    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir():
            continue
        slug = project_dir.name

        db_count = 0
        db_path = project_dir / ".bizniz" / "project.db"
        if db_path.exists():
            if args.dry_run:
                import sqlite3
                try:
                    conn = sqlite3.connect(str(db_path))
                    db_count = conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
                    conn.close()
                except Exception:
                    db_count = 0
            else:
                db_count = backfill_from_sqlite(db_path, slug, ledger=ledger)

        md_count = 0
        for run_dir in (project_dir / "docs" / "runs").glob("*/"):
            md = run_dir / "costs.md"
            if md.exists():
                if args.dry_run:
                    from bizniz.cost.audit import parse_costs_md
                    md_count += len(parse_costs_md(md))
                else:
                    md_count += backfill_from_costs_md(md, slug, ledger=ledger)

        if db_count or md_count:
            print(f"{slug:<35s} {db_count:>8,d} {md_count:>10,d}")
        grand_db += db_count
        grand_md += md_count

    print(f"{'-' * 35:<35s} {'-' * 8:>8s} {'-' * 10:>10s}")
    print(f"{'TOTAL':<35s} {grand_db:>8,d} {grand_md:>10,d}")
    print(f"{'GRAND':<35s} {(grand_db + grand_md):>20,d}")
    if args.dry_run:
        print("\n(dry run — no writes)")
    else:
        print(f"\nLedger now at {ledger_path}")
        print(f"Run audit: PYTHONPATH=. .venv/bin/python examples/v2_audit.py --since YYYY-MM-DD")


if __name__ == "__main__":
    main()

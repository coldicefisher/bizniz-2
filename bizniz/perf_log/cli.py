"""CLI entry point for perf_log analysis.

    python -m bizniz.perf_log <build.log>
    python -m bizniz.perf_log <build.log> --markdown out.md
    python -m bizniz.perf_log <build.log> --json out.json
    python -m bizniz.perf_log <build.log> --markdown out.md --json out.json

With both flags, writes both files. Without either, prints
markdown to stdout (so you can `| less`).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bizniz.perf_log.aggregators import build_report
from bizniz.perf_log.formatters import format_json, format_markdown
from bizniz.perf_log.parser import parse_log_file


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="bizniz.perf_log",
        description=(
            "Mine a v2_build log for performance + reliability metrics. "
            "Phase 1 (regex-based parser). Phase 2 (structured emit) "
            "lands with roadmap item 8."
        ),
    )
    p.add_argument(
        "log_file", type=Path,
        help="Path to a v2_build build log (e.g. /tmp/<project>.log).",
    )
    p.add_argument(
        "--markdown", type=Path, default=None,
        help="Write the markdown report to this path.",
    )
    p.add_argument(
        "--json", dest="json_path", type=Path, default=None,
        help="Write the structured JSON report to this path.",
    )
    args = p.parse_args(argv)

    log_path = args.log_file.expanduser().resolve()
    if not log_path.is_file():
        print(f"ERROR: log file not found at {log_path}", file=sys.stderr)
        return 2

    events = parse_log_file(log_path)
    report = build_report(events, source_path=str(log_path))

    wrote_to_file = False
    if args.markdown is not None:
        args.markdown.write_text(format_markdown(report))
        print(f"  wrote markdown report → {args.markdown}", file=sys.stderr)
        wrote_to_file = True
    if args.json_path is not None:
        args.json_path.write_text(format_json(report))
        print(f"  wrote JSON report     → {args.json_path}", file=sys.stderr)
        wrote_to_file = True
    if not wrote_to_file:
        # Default: dump markdown to stdout.
        print(format_markdown(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())

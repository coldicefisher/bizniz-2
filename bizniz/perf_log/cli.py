"""CLI entry point for perf_log analysis.

Single-build:

    python -m bizniz.perf_log <build.log>
    python -m bizniz.perf_log <build.log> --markdown out.md
    python -m bizniz.perf_log <build.log> --json out.json
    python -m bizniz.perf_log <build.log> --markdown out.md --json out.json

A/B comparison (--compare takes two log paths: baseline, candidate):

    python -m bizniz.perf_log --compare baseline.log candidate.log
    python -m bizniz.perf_log --compare a.log b.log --markdown cmp.md
    python -m bizniz.perf_log --compare a.log b.log --json cmp.json

In either mode, without --markdown/--json, the markdown report is
printed to stdout.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bizniz.perf_log.aggregators import build_report
from bizniz.perf_log.comparison import build_comparison
from bizniz.perf_log.formatters import (
    format_comparison_markdown,
    format_json,
    format_markdown,
)
from bizniz.perf_log.parser import parse_log_file


def _resolve_log(path: Path) -> Path | None:
    p = path.expanduser().resolve()
    if not p.is_file():
        print(f"ERROR: log file not found at {p}", file=sys.stderr)
        return None
    return p


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
        "log_file", type=Path, nargs="?",
        help="Path to a v2_build build log (single-build mode).",
    )
    p.add_argument(
        "--compare", nargs=2, metavar=("BASELINE", "CANDIDATE"), default=None,
        help="A/B comparison mode: BASELINE log vs CANDIDATE log.",
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

    if args.compare is None and args.log_file is None:
        p.error("must pass a log_file (single-build) or --compare A B")

    if args.compare is not None:
        baseline_path = _resolve_log(Path(args.compare[0]))
        candidate_path = _resolve_log(Path(args.compare[1]))
        if baseline_path is None or candidate_path is None:
            return 2
        baseline = build_report(
            parse_log_file(baseline_path),
            source_path=str(baseline_path),
        )
        candidate = build_report(
            parse_log_file(candidate_path),
            source_path=str(candidate_path),
        )
        report_obj = build_comparison(baseline, candidate)
        md = format_comparison_markdown(report_obj)
    else:
        log_path = _resolve_log(args.log_file)
        if log_path is None:
            return 2
        events = parse_log_file(log_path)
        report_obj = build_report(events, source_path=str(log_path))
        md = format_markdown(report_obj)

    wrote_to_file = False
    if args.markdown is not None:
        args.markdown.write_text(md)
        print(f"  wrote markdown report → {args.markdown}", file=sys.stderr)
        wrote_to_file = True
    if args.json_path is not None:
        args.json_path.write_text(format_json(report_obj))
        print(f"  wrote JSON report     → {args.json_path}", file=sys.stderr)
        wrote_to_file = True
    if not wrote_to_file:
        print(md)

    return 0


if __name__ == "__main__":
    sys.exit(main())

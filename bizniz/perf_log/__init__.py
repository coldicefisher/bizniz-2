"""perf_log — mine build logs for performance + reliability metrics.

Reads a v2_build log file (the ``/tmp/<project>.log`` produced by
``tee``) and emits a structured summary: per-agent timings,
per-milestone breakdown, decomposition stats, failure modes,
resume savings.

Phase 1 (this session): regex-based log parser. Works on existing
logs from today's crm_v1 build + any prior build. Fragile because
log format changes break the parser.

Phase 2 (roadmap item 8 proper): every agent emits structured
events.jsonl directly. Same event + aggregator types, different
ingestion source.

Usage:
    python -m bizniz.perf_log /tmp/crm_v1.log
    python -m bizniz.perf_log /tmp/crm_v1.log --json out.json
    python -m bizniz.perf_log /tmp/crm_v1.log --markdown report.md
"""
from bizniz.perf_log.aggregators import build_report
from bizniz.perf_log.events import (
    AgentCall,
    DecomposerResult,
    Event,
    MilestoneDone,
    UnitDispatch,
    UnitSkip,
)
from bizniz.perf_log.formatters import format_json, format_markdown
from bizniz.perf_log.parser import parse_log_file

__all__ = [
    "AgentCall",
    "DecomposerResult",
    "Event",
    "MilestoneDone",
    "UnitDispatch",
    "UnitSkip",
    "build_report",
    "format_json",
    "format_markdown",
    "parse_log_file",
]

"""Read structured perf events (Phase 2B emitter output) and
convert them to Phase 1 analyzer events.

The Phase 1 ``perf_log/parser.py`` reads prose log lines via regex
and produces ``perf_log/events.py`` types. The Phase 2 emitter
(``perf_log/emitter.py``) writes JSONL events directly. To keep
the aggregator + comparison + formatters unchanged, this module
translates Phase 2 → Phase 1 event-by-event and exposes the same
interface ``parse_log_file`` provides.

Phase 2 events without a Phase 1 equivalent (CacheHitEvent,
PhaseStartEvent, ToolUseEvent, AgentRetryEvent) are dropped on
the floor for now — the aggregator doesn't know what to do with
them yet. As the report shape grows to surface these (e.g.,
cache hit rates, retry distributions), the translator picks them
up.

Auto-detection: ``parse_run_artifacts(run_dir)`` prefers
``perf_events.jsonl`` if present; else falls back to
``build.log`` regex parsing. Same ``List[Event]`` output either
way — the rest of the pipeline doesn't care which source ran.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from bizniz.perf_log.events import (
    AgentCall,
    DecomposerResult,
    Event,
    GateEvent,
    MilestoneDone,
    RateLimitEvent,
    ReadonlyRetryEvent,
    SmokeRecoveryEvent,
)


def translate_emitter_event(record: dict) -> Optional[Event]:
    """Translate one emitter JSONL record to a Phase 1 Event.

    Returns ``None`` for record types Phase 1 doesn't aggregate yet
    (cache hits, phase boundaries, agent retries, tool uses).
    """
    et = record.get("event_type")
    elapsed_s = float(record.get("elapsed_s") or 0.0)
    ts = record.get("timestamp_iso") or ""

    if et == "agent_call":
        return AgentCall(
            elapsed_s=elapsed_s,
            timestamp=ts,
            raw="(from emitter)",
            agent=str(record.get("agent") or "Unknown"),
            target=str(record.get("target") or ""),
            duration_s=float(record.get("duration_s") or 0.0),
            response_chars=int(record.get("response_chars") or 0),
        )

    if et == "decomposer_result":
        return DecomposerResult(
            elapsed_s=elapsed_s,
            timestamp=ts,
            raw="(from emitter)",
            issue_id=str(record.get("issue_id") or ""),
            unit_count=int(record.get("unit_count") or 0),
            confidence=float(record.get("confidence") or 0.0),
        )

    if et == "milestone_done":
        return MilestoneDone(
            elapsed_s=elapsed_s,
            timestamp=ts,
            raw="(from emitter)",
            milestone_index=int(record.get("milestone_index") or 0),
            milestone_name=str(record.get("milestone_name") or ""),
            repair_iterations=int(record.get("repair_iterations") or 0),
        )

    if et == "gate_failure":
        sev_raw = record.get("severity") or "fail"
        sev = sev_raw if sev_raw in ("fail", "warn", "halt") else "fail"
        return GateEvent(
            elapsed_s=elapsed_s,
            timestamp=ts,
            raw="(from emitter)",
            gate_name=str(record.get("gate_name") or ""),
            severity=sev,
            reason=str(record.get("reason") or ""),
        )

    if et == "smoke_recovery":
        return SmokeRecoveryEvent(
            elapsed_s=elapsed_s,
            timestamp=ts,
            raw="(from emitter)",
            duration_s=float(record.get("duration_s") or 0.0),
            actions_count=int(record.get("actions_count") or 0),
            self_reported_ok=bool(record.get("self_reported_ok") or False),
        )

    if et == "readonly_retry":
        return ReadonlyRetryEvent(
            elapsed_s=elapsed_s,
            timestamp=ts,
            raw="(from emitter)",
        )

    if et == "rate_limit":
        detail_raw = record.get("detail") or "transient"
        detail = (
            detail_raw if detail_raw in ("usage_cap", "transient")
            else "transient"
        )
        return RateLimitEvent(
            elapsed_s=elapsed_s,
            timestamp=ts,
            raw="(from emitter)",
            detail=detail,
            wait_s=float(record.get("wait_s") or 0.0),
        )

    # Phase 2 events with no Phase 1 equivalent yet:
    # cache_hit, cache_miss, phase_start, phase_end, tool_use,
    # agent_retry. Aggregator will gain support as the report shape
    # grows.
    return None


def parse_emitter_jsonl(path: Path) -> List[Event]:
    """Read a ``perf_events.jsonl`` file emitted by the Phase 2
    ``PerfEmitter`` and translate each line into a Phase 1 Event.

    Lines that don't parse as JSON, or whose ``event_type`` has no
    Phase 1 equivalent, are skipped silently. Returns an empty list
    if the file is missing.
    """
    if not path.is_file():
        return []
    events: List[Event] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            ev = translate_emitter_event(record)
            if ev is not None:
                events.append(ev)
    return events


def parse_run_artifacts(
    run_dir: Path,
    log_path: Optional[Path] = None,
) -> List[Event]:
    """High-level reader: prefer Phase 2 structured events when
    available; fall back to Phase 1 regex parsing of ``log_path``.

    - ``run_dir`` — typically ``<project>/.bizniz/runs/<job_id>/``
    - ``log_path`` — optional path to the build log file. If
      omitted and no JSONL exists, returns empty list.
    """
    structured = run_dir / "perf_events.jsonl"
    if structured.is_file():
        events = parse_emitter_jsonl(structured)
        if events:
            return events
        # File exists but is empty — caller may still want the log
        # fallback rather than an empty report.
    if log_path is not None and log_path.is_file():
        # Lazy import — Phase 1 parser brings in regex compile cost.
        from bizniz.perf_log.parser import parse_log_file
        return parse_log_file(log_path)
    return []

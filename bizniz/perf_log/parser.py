"""Regex parser — log file → list of structured Events.

Phase-1 ingestion path. The parser is intentionally pattern-driven
(not stream-based) — small, focused, easy to extend with new
patterns as the log format grows. Each pattern is a tuple of
(compiled regex, factory function) that converts a match to an
Event.

Unknown lines are silently ignored. The parser is designed to be
robust to log format drift: a missing pattern means a missing
event, not a crash.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from bizniz.perf_log.events import (
    AgentCall,
    DecomposerResult,
    Event,
    GateEvent,
    MilestoneDone,
    ProUXDesignerTiming,
    RateLimitEvent,
    ReadonlyRetryEvent,
    SmokeRecoveryEvent,
    UnitDispatch,
    UnitSkip,
)


# ── Timestamp utilities ──────────────────────────────────────────


_TS_RE = re.compile(r"^\s*\[(\d{2}):(\d{2}):(\d{2})\]\s+(.*)$")


def _parse_timestamp(line: str) -> Optional[Tuple[str, str]]:
    """Return (HH:MM:SS, body) or None if the line doesn't start with
    a timestamp."""
    m = _TS_RE.match(line)
    if not m:
        return None
    hh, mm, ss, body = m.groups()
    return f"{hh}:{mm}:{ss}", body


def _ts_to_seconds(ts: str) -> int:
    """Convert ``HH:MM:SS`` to seconds-of-day (0..86399)."""
    hh, mm, ss = ts.split(":")
    return int(hh) * 3600 + int(mm) * 60 + int(ss)


# ── Pattern definitions ──────────────────────────────────────────


def _parent_issue_of(unit_id: str) -> str:
    """``BE-001-U2`` → ``BE-001``. ``BE-005-fix1`` → ``BE-005``."""
    # Strip trailing -Uxxx or -fixN suffix.
    m = re.match(r"^(.+?)(?:-[Uu]\d+|-fix\d+(?:-\d+)?)$", unit_id)
    if m:
        return m.group(1)
    return unit_id


# Decomposer: BE-001 → 1 unit(s), confidence=0.90
_RE_DECOMPOSER_RESULT = re.compile(
    r"^Decomposer:\s+([A-Z]+-\d+(?:-[A-Z]+)?)\s+→\s+(\d+)\s+unit\(s\),\s+confidence=([\d.]+)"
)

# Decomposer.BE-001: AI responded in 15.4s (2205 chars)
_RE_DECOMPOSER_CALL = re.compile(
    r"^Decomposer\.([A-Z]+-\d+):\s+AI responded in ([\d.]+)s\s+\((\d+)\s+chars\)"
)

# ServicePlanner(backend): AI responded in 81.3s (17871 chars)
_RE_SVC_PLANNER_CALL = re.compile(
    r"^ServicePlanner\(([a-z_]+)\):\s+AI responded in ([\d.]+)s\s+\((\d+)\s+chars\)"
)

# ServicePlanner.repair(backend, iter1): AI responded in ...
_RE_SVC_PLANNER_REPAIR = re.compile(
    r"^ServicePlanner\.repair\(([a-z_]+),\s+iter\d+\):\s+AI responded in ([\d.]+)s\s+\((\d+)\s+chars\)"
)

# QualityEngineer.enrich: AI responded in 81.3s (17871 chars)
# QualityEngineer.review: AI responded in 77.4s (18036 chars)
_RE_QE_CALL = re.compile(
    r"^QualityEngineer\.(\w+):\s+AI responded in ([\d.]+)s\s+\((\d+)\s+chars\)"
)

# ClaudeCliCoder: BE-002-U2 subprocess done in 131.9s (exit 0)
_RE_CODER_DONE = re.compile(
    r"^ClaudeCliCoder:\s+([A-Z]+-\d+(?:-[A-Z]+\d+|-fix\d+(?:-\d+)?)?)\s+subprocess done in ([\d.]+)s\s+\(exit\s+(-?\d+)\)"
)

# [backend] BE-009-U1: resume — already passed on previous run, skipping
_RE_UNIT_SKIP = re.compile(
    r"^\[([a-z_]+)\]\s+([A-Z]+-\d+(?:-[A-Z]+\d+)?):\s+resume\s+—\s+already passed"
)

# MilestoneLoop: M2 'Contacts CRUD and search' DONE (1 repair iterations)
_RE_MILESTONE_DONE = re.compile(
    r"^MilestoneLoop:\s+M(\d+)\s+'([^']+)'\s+DONE\s+\((\d+)\s+repair iterations\)"
)

# ProUXDesigner: timing — total=3972.5s, fix=1347.2s, global_design=939.1s, ...
_RE_UX_TIMING = re.compile(
    r"^ProUXDesigner:\s+timing\s+—\s+(.+)$"
)

# GATE FAIL [smoke_failed]: ...
_RE_GATE_FAIL = re.compile(
    r"^GATE\s+(FAIL|WARN|PAUSE)\s+\[([^\]]+)\]:\s+(.*)$"
)

# V2Pipeline halted at gate 'smoke_failed': ...
_RE_GATE_HALT = re.compile(
    r"^V2Pipeline halted at gate '([^']+)':\s+(.*)$"
)

# SmokeRecovery: returned in 91.1s — 1 action(s); self_reported_ok=True
_RE_SMOKE_RECOVERY = re.compile(
    r"^SmokeRecovery:\s+returned in ([\d.]+)s\s+—\s+(\d+)\s+action\(s\);\s+self_reported_ok=(True|False)"
)

# Max-plan usage cap hit, sleeping 1234s
_RE_RATE_LIMIT_USAGE = re.compile(
    r"^\[ClaudeCliClient\]\s+Max-plan usage cap hit,\s+sleeping\s+([\d.]+)s"
)

# transient 429 (no reset time), backing off 10s ...
_RE_RATE_LIMIT_TRANSIENT = re.compile(
    r"^\[ClaudeCliClient\]\s+transient 429.*backing off\s+([\d.]+)s"
)

# [ProjectDB] readonly-database OperationalError; reconnecting and retrying once...
_RE_READONLY_RETRY = re.compile(
    r"^\[ProjectDB\]\s+readonly-database OperationalError"
)


def _parse_ux_phase_timings(body: str) -> dict:
    """Parse ``total=3972.5s, fix=1347.2s, global_design=939.1s`` into
    a dict, dropping the trailing 's'."""
    out: dict = {}
    for chunk in body.split(","):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        v = v.strip().rstrip("s")
        try:
            out[k.strip()] = float(v)
        except ValueError:
            continue
    return out


# ── Recognizer dispatch ──────────────────────────────────────────


def _recognize(body: str) -> Optional[Event]:
    """Try every recognizer in order. Returns the first match's
    event, or None if no pattern matched.

    Order matters when patterns could overlap — most-specific first.
    """
    m = _RE_DECOMPOSER_RESULT.match(body)
    if m:
        issue_id, units, conf = m.groups()
        return DecomposerResult(
            issue_id=issue_id,
            unit_count=int(units),
            confidence=float(conf),
        )

    m = _RE_DECOMPOSER_CALL.match(body)
    if m:
        target, dur, chars = m.groups()
        return AgentCall(
            agent="Decomposer", target=target,
            duration_s=float(dur), response_chars=int(chars),
        )

    m = _RE_SVC_PLANNER_REPAIR.match(body)
    if m:
        svc, dur, chars = m.groups()
        return AgentCall(
            agent="ServicePlanner.repair", target=svc,
            duration_s=float(dur), response_chars=int(chars),
        )

    m = _RE_SVC_PLANNER_CALL.match(body)
    if m:
        svc, dur, chars = m.groups()
        return AgentCall(
            agent="ServicePlanner", target=svc,
            duration_s=float(dur), response_chars=int(chars),
        )

    m = _RE_QE_CALL.match(body)
    if m:
        kind, dur, chars = m.groups()
        return AgentCall(
            agent=f"QualityEngineer.{kind}", target="",
            duration_s=float(dur), response_chars=int(chars),
        )

    m = _RE_CODER_DONE.match(body)
    if m:
        unit_id, dur, exit_code = m.groups()
        return UnitDispatch(
            unit_id=unit_id, duration_s=float(dur),
            exit_code=int(exit_code),
            parent_issue=_parent_issue_of(unit_id),
        )

    m = _RE_UNIT_SKIP.match(body)
    if m:
        svc, unit_id = m.groups()
        return UnitSkip(
            unit_id=unit_id, service=svc,
            parent_issue=_parent_issue_of(unit_id),
        )

    m = _RE_MILESTONE_DONE.match(body)
    if m:
        idx, name, repairs = m.groups()
        return MilestoneDone(
            milestone_index=int(idx),
            milestone_name=name,
            repair_iterations=int(repairs),
        )

    m = _RE_UX_TIMING.match(body)
    if m:
        timings = _parse_ux_phase_timings(m.group(1))
        return ProUXDesignerTiming(
            total_s=timings.pop("total", 0.0),
            phase_timings=timings,
        )

    m = _RE_GATE_FAIL.match(body)
    if m:
        sev_token, gate, reason = m.groups()
        sev_token = sev_token.lower()
        sev = "fail" if sev_token == "fail" else (
            "halt" if sev_token == "pause" else "warn"
        )
        return GateEvent(gate_name=gate, severity=sev, reason=reason)

    m = _RE_GATE_HALT.match(body)
    if m:
        gate, reason = m.groups()
        return GateEvent(gate_name=gate, severity="halt", reason=reason)

    m = _RE_SMOKE_RECOVERY.match(body)
    if m:
        dur, count, ok = m.groups()
        return SmokeRecoveryEvent(
            duration_s=float(dur),
            actions_count=int(count),
            self_reported_ok=(ok == "True"),
        )

    m = _RE_RATE_LIMIT_USAGE.match(body)
    if m:
        return RateLimitEvent(detail="usage_cap", wait_s=float(m.group(1)))

    m = _RE_RATE_LIMIT_TRANSIENT.match(body)
    if m:
        return RateLimitEvent(detail="transient", wait_s=float(m.group(1)))

    if _RE_READONLY_RETRY.match(body):
        return ReadonlyRetryEvent()

    return None


# ── Public API ───────────────────────────────────────────────────


def parse_log_lines(lines: Iterable[str]) -> List[Event]:
    """Parse an iterable of log lines into events. Times become
    elapsed seconds from the first parsed timestamp; lines that
    cross midnight (current_seconds < previous_seconds) trigger a
    day increment so durations stay monotonic."""
    events: List[Event] = []
    first_seconds: Optional[int] = None
    prev_seconds: Optional[int] = None
    day_offset = 0

    for raw in lines:
        ts_body = _parse_timestamp(raw)
        if ts_body is None:
            continue
        ts, body = ts_body
        sec = _ts_to_seconds(ts)
        if first_seconds is None:
            first_seconds = sec
        if prev_seconds is not None and sec < prev_seconds - 60:
            # Day rolled over (HH:MM:SS reset). Bump offset.
            day_offset += 86400
        prev_seconds = sec
        elapsed = (sec + day_offset) - first_seconds
        # Body can have leading "[..]" prefixes — strip them.
        body_clean = body.strip()
        ev = _recognize(body_clean)
        if ev is not None:
            ev.elapsed_s = float(elapsed)
            ev.timestamp = ts
            ev.raw = body_clean[:300]
            events.append(ev)
    return events


def parse_log_file(path) -> List[Event]:
    """Parse a build log file by path."""
    p = Path(path)
    with p.open("r", encoding="utf-8", errors="ignore") as f:
        return parse_log_lines(f)

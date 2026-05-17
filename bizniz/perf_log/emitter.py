"""Structured perf-event emitter — Phase 2 of roadmap item 9.

Where Phase 1's analyzer (``perf_log/parser.py``) regex-mines prose
log lines for metrics, Phase 2 has the agents EMIT structured
events directly to a JSONL file. The analyzer reads either source.

Phase 1 stays — it's the fallback for builds where the emitter
isn't wired and for archived logs that predate the migration.
Phase 2 is the source of truth going forward: regex parsing is
brittle (log format changes break it) and can't capture metrics
that aren't logged (token counts, cache hits, etc.).

Why JSONL: one event per line, atomic append-only writes survive
crashes mid-emit, every event self-contained (no cross-line
parsing). Compatible with ``jq``, ``grep``, perf_log's analyzer,
and future tools without schema migration.

Event location: ``<runs_root>/<job_id>/perf_events.jsonl`` —
sibling to the existing per-phase JSON dumps so resume + post-
mortem find them naturally.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# ── Event schemas ────────────────────────────────────────────────


# Discriminator for the union — every event has ``event_type``.
EventType = Literal[
    "agent_call",      # one LLM call through call_with_retry
    "agent_retry",     # one transient retry within a call
    "tool_use",        # one tool invocation inside a tool loop
    "cache_hit",       # plan/route/design-lock cache hit
    "cache_miss",      # plan/route/design-lock cache miss
    "phase_start",     # SubPhase boundary marker
    "phase_end",
    "gate_failure",    # smoke/integration/post-integration gate failed
    "smoke_recovery",  # SmokeRecovery agent attempted recovery
    "readonly_retry",  # ProjectDB readonly OperationalError retry
    "rate_limit",      # 429/usage-cap wait
    "decomposer_result",  # one Decomposer.decompose call result
    "milestone_done",
]


class _EventBase(BaseModel):
    """Fields every event carries."""
    event_type: EventType
    timestamp_iso: str = Field(
        default_factory=lambda: datetime.datetime.now(
            datetime.timezone.utc,
        ).isoformat(),
    )
    elapsed_s: float = 0.0   # wall-clock since run start
    job_id: Optional[str] = None
    milestone_index: Optional[int] = None


class AgentCallEvent(_EventBase):
    """One LLM call (the unit of work for single-call agents)."""
    event_type: Literal["agent_call"] = "agent_call"
    agent: str
    target: Optional[str] = None     # e.g., service name, issue id
    model: Optional[str] = None
    duration_s: float = 0.0
    succeeded: bool = True
    response_chars: int = 0
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    permanent_attempts: int = 0
    transient_attempts: int = 0


class AgentRetryEvent(_EventBase):
    """One retry within an agent call (transient backoff or permanent reroll)."""
    event_type: Literal["agent_retry"] = "agent_retry"
    agent: str
    target: Optional[str] = None
    attempt_index: int = 0
    classification: Literal["transient", "permanent"] = "permanent"
    wait_s: float = 0.0
    error: str = ""


class ToolUseEvent(_EventBase):
    """One tool invocation inside a tool-loop agent."""
    event_type: Literal["tool_use"] = "tool_use"
    agent: str
    tool: str
    duration_s: float = 0.0
    succeeded: bool = True


class CacheHitEvent(_EventBase):
    event_type: Literal["cache_hit"] = "cache_hit"
    cache_name: str
    key: str = ""


class CacheMissEvent(_EventBase):
    event_type: Literal["cache_miss"] = "cache_miss"
    cache_name: str
    key: str = ""
    reason: str = ""


class PhaseStartEvent(_EventBase):
    event_type: Literal["phase_start"] = "phase_start"
    phase: str


class PhaseEndEvent(_EventBase):
    event_type: Literal["phase_end"] = "phase_end"
    phase: str
    duration_s: float = 0.0
    passed: bool = True


class GateFailureEvent(_EventBase):
    event_type: Literal["gate_failure"] = "gate_failure"
    gate_name: str
    severity: Literal["fail", "warn", "halt"] = "fail"
    reason: str = ""


class SmokeRecoveryEvent(_EventBase):
    event_type: Literal["smoke_recovery"] = "smoke_recovery"
    duration_s: float = 0.0
    actions_count: int = 0
    self_reported_ok: bool = False


class ReadonlyRetryEvent(_EventBase):
    event_type: Literal["readonly_retry"] = "readonly_retry"
    shape: str = ""   # "readonly-database", "database-locked", ...


class RateLimitEvent(_EventBase):
    event_type: Literal["rate_limit"] = "rate_limit"
    detail: Literal["usage_cap", "transient", "other"] = "transient"
    wait_s: float = 0.0


class DecomposerResultEvent(_EventBase):
    event_type: Literal["decomposer_result"] = "decomposer_result"
    issue_id: str
    unit_count: int = 0
    confidence: float = 0.0


class MilestoneDoneEvent(_EventBase):
    event_type: Literal["milestone_done"] = "milestone_done"
    milestone_name: str = ""
    repair_iterations: int = 0


# Union for downstream consumers (analyzer, comparison).
PerfEvent = Union[
    AgentCallEvent, AgentRetryEvent, ToolUseEvent,
    CacheHitEvent, CacheMissEvent,
    PhaseStartEvent, PhaseEndEvent,
    GateFailureEvent, SmokeRecoveryEvent,
    ReadonlyRetryEvent, RateLimitEvent,
    DecomposerResultEvent, MilestoneDoneEvent,
]


# ── Emitter ──────────────────────────────────────────────────────


class PerfEmitter:
    """JSONL event emitter.

    Thread-safe: ``emit()`` takes a mutex around the file write so
    concurrent agents can't corrupt the file. The write itself is
    a single ``f.write(line)`` followed by ``flush()``, which is
    atomic for POSIX appends under the typical line size.

    Either:
    - Pass ``output_path`` (file mode — production)
    - Pass ``in_memory=True`` (test mode — events accumulate in
      ``.collected`` for inspection without disk I/O)
    """

    def __init__(
        self,
        output_path: Optional[Path] = None,
        in_memory: bool = False,
        job_id: Optional[str] = None,
        on_status: Optional[Callable[[str], None]] = None,
        time_source: Optional[Callable[[], float]] = None,
    ) -> None:
        if output_path is None and not in_memory:
            raise ValueError(
                "PerfEmitter: must supply either output_path or "
                "in_memory=True"
            )
        if output_path is not None and in_memory:
            raise ValueError(
                "PerfEmitter: output_path and in_memory are mutually "
                "exclusive"
            )
        self._output_path = output_path
        self._in_memory = in_memory
        self.collected: List[PerfEvent] = []
        self._job_id = job_id
        self._on_status = on_status
        self._time_source = time_source
        self._start_time = self._now() if time_source else None
        self._lock = threading.Lock()
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            # Truncate at start so resume runs append to a fresh file.
            # (Existing perf data for this job_id is in the old file's
            # path, which the analyzer can read separately if needed.)
            if not output_path.exists():
                output_path.touch()

    def _now(self) -> float:
        return self._time_source() if self._time_source else 0.0

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    @property
    def start_time(self) -> Optional[float]:
        return self._start_time

    def emit(self, event: PerfEvent) -> None:
        """Stamp the event with job_id + elapsed_s + flush to disk."""
        # Stamp common fields if not already set.
        if event.job_id is None and self._job_id is not None:
            event.job_id = self._job_id
        if event.elapsed_s == 0.0 and self._start_time is not None:
            event.elapsed_s = max(0.0, self._now() - self._start_time)

        if self._in_memory:
            with self._lock:
                self.collected.append(event)
            return

        # Disk mode.
        line = event.model_dump_json() + "\n"
        with self._lock:
            try:
                with open(self._output_path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
            except OSError as e:
                self._log(
                    f"PerfEmitter: write failed ({type(e).__name__}: "
                    f"{e}) — dropping event"
                )

    # ── Convenience emitters ─────────────────────────────────────

    def agent_call(
        self, *, agent: str, target: Optional[str] = None,
        model: Optional[str] = None, duration_s: float = 0.0,
        succeeded: bool = True, response_chars: int = 0,
        tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
        permanent_attempts: int = 0, transient_attempts: int = 0,
    ) -> None:
        self.emit(AgentCallEvent(
            agent=agent, target=target, model=model,
            duration_s=duration_s, succeeded=succeeded,
            response_chars=response_chars,
            tokens_in=tokens_in, tokens_out=tokens_out,
            permanent_attempts=permanent_attempts,
            transient_attempts=transient_attempts,
        ))

    def agent_retry(
        self, *, agent: str, target: Optional[str] = None,
        attempt_index: int = 0,
        classification: str = "permanent",
        wait_s: float = 0.0, error: str = "",
    ) -> None:
        cls: Any = classification if classification in ("transient", "permanent") else "permanent"
        self.emit(AgentRetryEvent(
            agent=agent, target=target,
            attempt_index=attempt_index,
            classification=cls, wait_s=wait_s, error=error,
        ))

    def cache_hit(self, *, cache_name: str, key: str = "") -> None:
        self.emit(CacheHitEvent(cache_name=cache_name, key=key))

    def cache_miss(self, *, cache_name: str, key: str = "", reason: str = "") -> None:
        self.emit(CacheMissEvent(cache_name=cache_name, key=key, reason=reason))

    def phase_start(self, *, phase: str) -> None:
        self.emit(PhaseStartEvent(phase=phase))

    def phase_end(self, *, phase: str, duration_s: float = 0.0, passed: bool = True) -> None:
        self.emit(PhaseEndEvent(phase=phase, duration_s=duration_s, passed=passed))

    def gate_failure(
        self, *, gate_name: str,
        severity: str = "fail", reason: str = "",
    ) -> None:
        sev: Any = severity if severity in ("fail", "warn", "halt") else "fail"
        self.emit(GateFailureEvent(
            gate_name=gate_name, severity=sev, reason=reason,
        ))

    def readonly_retry(self, *, shape: str = "readonly-database") -> None:
        self.emit(ReadonlyRetryEvent(shape=shape))

    def rate_limit(
        self, *, detail: str = "transient", wait_s: float = 0.0,
    ) -> None:
        d: Any = detail if detail in ("usage_cap", "transient", "other") else "transient"
        self.emit(RateLimitEvent(detail=d, wait_s=wait_s))

    def decomposer_result(
        self, *, issue_id: str, unit_count: int, confidence: float,
    ) -> None:
        self.emit(DecomposerResultEvent(
            issue_id=issue_id, unit_count=unit_count,
            confidence=confidence,
        ))

    def milestone_done(
        self, *, milestone_index: int, milestone_name: str,
        repair_iterations: int = 0,
    ) -> None:
        self.emit(MilestoneDoneEvent(
            milestone_index=milestone_index,
            milestone_name=milestone_name,
            repair_iterations=repair_iterations,
        ))


# ── Null emitter (default for code that hasn't been wired) ───────


class NullPerfEmitter(PerfEmitter):
    """No-op emitter — every method swallows. Default when the
    pipeline isn't wired to a real emitter. Lets agents call
    ``emitter.agent_call(...)`` unconditionally without nullity
    checks."""

    def __init__(self) -> None:
        self._output_path = None
        self._in_memory = True
        self.collected: List[PerfEvent] = []
        self._job_id = None
        self._on_status = None
        self._time_source = None
        self._start_time = 0.0
        self._lock = threading.Lock()

    def emit(self, event: PerfEvent) -> None:
        return  # noqa: WPS420

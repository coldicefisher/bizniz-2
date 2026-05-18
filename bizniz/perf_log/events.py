"""Event types — the structured representation of a build log.

Each interesting log line becomes one of these. The parser produces
a list of events; aggregators consume them to compute summaries.
"""
from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field


class _BaseEvent(BaseModel):
    """Common fields on every event."""
    # Relative seconds since the first log line we saw. Lets us
    # compare durations + sequencing without dealing with absolute
    # timestamps (log lines are `[HH:MM:SS]` only, no date).
    elapsed_s: float = 0.0
    # Original timestamp string `HH:MM:SS` for round-tripping back
    # into output.
    timestamp: str = ""
    # Raw log line (truncated) for diagnostics.
    raw: str = ""


class AgentCall(_BaseEvent):
    """A single-call agent (ServicePlanner, QualityEngineer.enrich,
    Decomposer, Architect, Planner, etc.) responded to a request.

    Captures from log lines like:
      [21:32:35] Decomposer.BE-001: AI responded in 15.4s (2205 chars)
      [21:32:20] ServicePlanner(backend): AI responded in 81.3s (17871 chars)
    """
    event_type: Literal["agent_call"] = "agent_call"
    agent: str = ""           # "Decomposer", "ServicePlanner", etc.
    target: str = ""          # "BE-001", "backend" — what it was for
    duration_s: float = 0.0
    response_chars: int = 0


class UnitDispatch(_BaseEvent):
    """A Coder subprocess for one unit/issue completed.

    From log lines like:
      [22:13:01] ClaudeCliCoder: BE-002-U2 subprocess done in 131.9s (exit 0)
    """
    event_type: Literal["unit_dispatch"] = "unit_dispatch"
    unit_id: str = ""
    duration_s: float = 0.0
    exit_code: int = 0
    parent_issue: str = ""    # derived from unit_id (BE-001-U2 → BE-001)


class UnitSkip(_BaseEvent):
    """A unit was skipped during dispatch because the issue store
    showed it already passed on a prior run.

    From log lines like:
      [21:38:41] [backend] BE-009-U1: resume — already passed on previous run, skipping
    """
    event_type: Literal["unit_skip"] = "unit_skip"
    unit_id: str = ""
    service: str = ""
    parent_issue: str = ""


class DecomposerResult(_BaseEvent):
    """Decomposer finished decomposing one issue into N units.

    From log lines like:
      [21:34:54] Decomposer: BE-004 → 7 unit(s), confidence=0.85
    """
    event_type: Literal["decomposer_result"] = "decomposer_result"
    issue_id: str = ""
    unit_count: int = 0
    confidence: float = 0.0


class MilestoneDone(_BaseEvent):
    """A milestone reached DONE.

    From log lines like:
      [10:46:52] MilestoneLoop: M2 'Contacts CRUD and search' DONE (1 repair iterations)
    """
    event_type: Literal["milestone_done"] = "milestone_done"
    milestone_index: int = 0
    milestone_name: str = ""
    repair_iterations: int = 0


class ProUXDesignerTiming(_BaseEvent):
    """End-of-UX-phase timing breakdown emitted by ProUXDesigner.

    From log lines like:
      ProUXDesigner: timing — total=3972.5s, fix=1347.2s, global_design=939.1s, ...
    """
    event_type: Literal["ux_timing"] = "ux_timing"
    total_s: float = 0.0
    phase_timings: dict = Field(default_factory=dict)


class GateEvent(_BaseEvent):
    """A pipeline gate fired (hard fail OR soft warn).

    From log lines like:
      [14:01:39] GATE FAIL [smoke_failed]: ...
      [14:01:39] V2Pipeline halted at gate 'smoke_failed': ...
    """
    event_type: Literal["gate"] = "gate"
    gate_name: str = ""
    severity: Literal["fail", "warn", "halt"] = "warn"
    reason: str = ""


class SmokeRecoveryEvent(_BaseEvent):
    """SmokeRecovery agent fired (and reported a result).

    From log lines like:
      SmokeRecovery: returned in 91.1s — 1 action(s); self_reported_ok=True
    """
    event_type: Literal["smoke_recovery"] = "smoke_recovery"
    duration_s: float = 0.0
    actions_count: int = 0
    self_reported_ok: bool = False


class RateLimitEvent(_BaseEvent):
    """A rate-limit-related event (429 hit, reset window wait,
    fallback model trigger).

    From log lines like:
      ClaudeCliClient 429 rate-limit hit, ...
      Max-plan usage cap hit, sleeping ...
    """
    event_type: Literal["rate_limit"] = "rate_limit"
    detail: str = ""
    wait_s: float = 0.0


class ReadonlyRetryEvent(_BaseEvent):
    """The project_db retry-with-reconnect wrapper fired.

    From log lines like:
      [ProjectDB] readonly-database OperationalError; reconnecting and retrying once...
    """
    event_type: Literal["readonly_retry"] = "readonly_retry"


class SubprocessCall(_BaseEvent):
    """A tool-loop subprocess agent (ClaudeCliDebugger, Refactorer,
    HTTPApiTester, WebUITester, WorkerTester, IntegrationDebugger tier
    attempt) completed.

    Unlike single-call agents (which emit ``AI responded in Xs (N chars)``),
    these emit a ``subprocess done in Xs (exit N)``-style line because
    they shell out to ``claude --print`` themselves. The ``target``
    field carries the meaningful identity (service name, issue id,
    file path) depending on agent.

    From log lines like:
      [12:08:42] ClaudeCliDebugger: subprocess done in 412.3s (exit 0)
      [09:55:17] Refactorer: subprocess done in 198.1s (exit 0)
      [11:33:07] HTTPApiTester(backend): completed in 73.2s
      [11:48:22] WebUITester(frontend): completed in 184.6s
      [12:01:55] IntegrationDebugger[backend, opus-4-7 attempt 2]: 281.5s exit 0
    """
    event_type: Literal["subprocess_call"] = "subprocess_call"
    agent: str = ""           # "ClaudeCliDebugger", "Refactorer", etc.
    target: str = ""          # service/issue/file context
    duration_s: float = 0.0
    exit_code: int = 0        # 0 when the agent didn't report one


# Discriminated union of all event types for parser output.
Event = Union[
    AgentCall, UnitDispatch, UnitSkip, DecomposerResult,
    MilestoneDone, ProUXDesignerTiming, GateEvent,
    SmokeRecoveryEvent, RateLimitEvent, ReadonlyRetryEvent,
    SubprocessCall,
]

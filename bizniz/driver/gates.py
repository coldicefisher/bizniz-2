"""Gate policy — decides what to do when a phase fails or surfaces concerns.

Three modes:

  - ``strict``       (default) halt on hard gates, continue on soft gates
  - ``auto``         push through soft gates; halt on hard gates only
  - ``interactive``  halt on every gate (for human-gated runs)

Hard gates (always halt):
  - Engineer iteration cap reached without submit_implementation
  - Critical findings present after repair budget exhausted
  - Integration tests failed and AgenticDebugger could not repair
  - AuthAgent failed to configure FA correctly
  - Any uncaught exception in agent code

Soft gates (warn in strict/auto, halt in interactive):
  - QE.review confidence < 0.6
  - Warnings-only findings (zero critical)
  - More than 1 repair iteration consumed
"""
from __future__ import annotations

from enum import Enum
from typing import Callable, Optional


class GateAction(str, Enum):
    CONTINUE = "continue"
    HALT = "halt"
    WARN = "warn"


class GateViolation(Exception):
    """Raised by gate.halt() to abort the pipeline cleanly.

    Caller catches at the top level + persists state + exits non-zero.
    """
    def __init__(self, gate_name: str, reason: str, hard: bool = True):
        self.gate_name = gate_name
        self.reason = reason
        self.hard = hard
        super().__init__(f"[{gate_name}] {reason}")


class GatePolicy:
    """Single object the milestone loop consults at each gate."""

    def __init__(
        self,
        mode: str = "strict",
        on_status: Optional[Callable[[str], None]] = None,
    ):
        if mode not in ("strict", "auto", "interactive"):
            raise ValueError(f"invalid gate mode: {mode}")
        self._mode = mode
        self._on_status = on_status

    @property
    def mode(self) -> str:
        return self._mode

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    def hard(self, gate_name: str, reason: str) -> None:
        """Always halts. Use for unrecoverable failures."""
        self._log(f"GATE FAIL [{gate_name}]: {reason}")
        raise GateViolation(gate_name, reason, hard=True)

    def soft(self, gate_name: str, reason: str) -> GateAction:
        """Warn + continue in strict/auto; halt in interactive.

        Returns the action taken so callers can branch on it.
        """
        if self._mode == "interactive":
            self._log(f"GATE PAUSE [{gate_name}]: {reason}")
            raise GateViolation(gate_name, reason, hard=False)
        self._log(f"GATE WARN [{gate_name}]: {reason}")
        return GateAction.WARN

    def conditional(
        self, gate_name: str, *, hard: bool, reason: str,
    ) -> Optional[GateAction]:
        """Convenience: branch on whether the gate is hard or soft.

        Returns None if action was CONTINUE (no-op), otherwise raises.
        """
        if hard:
            self.hard(gate_name, reason)
            return None  # unreachable
        return self.soft(gate_name, reason)

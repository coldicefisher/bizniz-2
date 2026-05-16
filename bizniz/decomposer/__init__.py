"""Decomposer — break a coarse issue into ordered units of work.

Roadmap item 4. Sits between ServicePlanner (emits feature-sized
issues) and Coder (writes code per dispatch). Decomposer takes one
issue and emits an ordered list of ``UnitOfWork`` — each unit
bounded to one new exported symbol (or one new behavior on an
existing symbol).

Coder/Tester/Debugger loops at unit granularity instead of
issue granularity:

  - Smaller LLM call, less prompt context, tighter attention
  - Per-unit test signal → bounded debugger blast radius
  - Atomic refactor extractions (item 5)
  - Per-unit confidence-signal opportunity (item 1's pattern,
    extended in item 8)

Pluggable across LLM backends — same shape as our other
single-call agents (QualityEngineer, ServicePlanner, etc.).
"""
from bizniz.decomposer.agent import Decomposer, DecomposerError
from bizniz.decomposer.types import (
    DecompositionResult,
    UnitOfWork,
)

__all__ = [
    "Decomposer",
    "DecomposerError",
    "DecompositionResult",
    "UnitOfWork",
]

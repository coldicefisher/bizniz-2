"""Coder data types — Issue + CoderResult.

``Issue`` is what ServicePlanner emits and Coder consumes. Same fields
as v2 Engineer's Issue (we keep those proven semantics) plus a couple
of v2.5-specific fields:

  - ``service`` (str): which compose service this issue's code runs in
  - ``language`` (str): "python" / "typescript" — drives symbol_validator

``CoderResult`` is what Coder.code_issue returns to its dispatcher
(orchestrator). Includes status + tier reached so the dispatcher can
decide whether to escalate or move on.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


CoderStatus = Literal[
    "passed",     # tests written + run green
    "partial",    # code written but tests failed at exit
    "stalled",    # repetition detected → escalate tier
    "deferred",   # blocked outside this issue's scope
    "failed",     # ran out of options
]


class Issue(BaseModel):
    """One unit of work — written by ServicePlanner, consumed by Coder.

    ``spec_refs`` lists capability ids from the EnrichedSpec this
    issue delivers. ``depends_on`` lists OTHER Issue ids in the same
    service that must be coded first (so symbol_validator can resolve
    cross-issue imports).
    """
    id: str = Field(..., description="Stable id, e.g. 'BE-AUTH-001'.")
    title: str
    description: str
    service: str = Field(..., description="Compose service name this issue lives in")
    language: str = Field(default="python", description="'python' | 'typescript'")
    target_files: List[str] = Field(default_factory=list)
    test_files: List[str] = Field(default_factory=list)
    success_criteria: List[str] = Field(default_factory=list)
    spec_refs: List[str] = Field(default_factory=list)
    depends_on: List[str] = Field(default_factory=list)


class CoderResult(BaseModel):
    """Returned by Coder.code_issue() to its dispatcher (orchestrator)."""
    issue_id: str
    status: CoderStatus
    target_files_written: List[str] = Field(default_factory=list)
    test_files_written: List[str] = Field(default_factory=list)
    summary: str = ""
    notes: List[str] = Field(default_factory=list)
    tier_used: int = 0
    iterations_used: int = 0
    unresolved_symbols_at_exit: List[str] = Field(default_factory=list)
    last_test_output_tail: str = ""


class CoderError(Exception):
    """Coder failed in a way the dispatcher should know about.
    ``last_action`` carries the action sig if the failure was a stall."""
    def __init__(self, message: str, last_action: str = ""):
        super().__init__(message)
        self.last_action = last_action

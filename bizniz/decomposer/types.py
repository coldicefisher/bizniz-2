"""Pydantic types for the Decomposer agent.

UnitOfWork is the granular dispatch unit. DecompositionResult is
what Decomposer.decompose returns.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# What kind of work a unit performs. ``new_symbol`` creates a new
# exported function/class/component; ``new_behavior`` adds a method
# or prop to an existing symbol; ``bundled_boilerplate`` is pure
# scaffolding (imports, types, constants) that supports a sibling
# unit and doesn't need its own test.
UnitKind = Literal["new_symbol", "new_behavior", "bundled_boilerplate"]

# Whether a passing test is required before the loop advances past
# this unit. ``unit_test`` = a focused per-unit test must pass.
# ``no_test_needed`` = pure boilerplate / config / one-line constants
# where a test would be ceremony, not signal.
TestKind = Literal["unit_test", "no_test_needed"]


class UnitOfWork(BaseModel):
    """One granular dispatch unit within an issue.

    Sized so a single Coder call (~30-180s) implements + tests it.
    Multi-symbol units are a code smell — split them.
    """

    id: str = Field(
        ...,
        description=(
            "Stable unit id, parent issue id + suffix. "
            "E.g. ``BE-005-u1``, ``FE-007-u3``."
        ),
    )
    summary: str = Field(
        ...,
        description="One-sentence what-this-unit-does (display label).",
    )
    description: str = Field(
        ...,
        description=(
            "What to write + why. Concrete enough that a Coder can "
            "implement without re-reading the parent issue."
        ),
    )
    target_file: str = Field(
        ...,
        description=(
            "Workspace-relative path of the file this unit writes "
            "or modifies. Single file. If you need to touch two "
            "files, split into two units."
        ),
    )
    kind: UnitKind = Field(
        default="new_symbol",
        description=(
            "What kind of work — new_symbol (new export), "
            "new_behavior (added method/prop on existing), or "
            "bundled_boilerplate (imports / types / constants)."
        ),
    )
    depends_on: List[str] = Field(
        default_factory=list,
        description=(
            "Prior unit ids (in this issue) OR existing "
            "workspace-symbol paths (``app/models/user.py::User``) "
            "that must exist before this unit can be implemented. "
            "Dispatcher uses this to order the per-unit loop."
        ),
    )
    expected_test_kind: TestKind = Field(
        default="unit_test",
        description=(
            "Whether a passing per-unit test is required before "
            "advancing. ``no_test_needed`` reserved for pure "
            "boilerplate."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description=(
            "Optional caveats: tricky edge cases, fixture needs, "
            "rationale for the order choice."
        ),
    )


class DecompositionResult(BaseModel):
    """What Decomposer.decompose returns."""

    issue_id: str = Field(
        ...,
        description="The parent issue id (echoed for diagnostics).",
    )
    ordered_units: List[UnitOfWork] = Field(
        default_factory=list,
        description=(
            "Units in dependency order. Dispatcher walks this list "
            "sequentially; can later parallelize independent leaves."
        ),
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Decomposer's self-rated confidence in the decomposition "
            "quality. Same load-bearing pattern as QE.enrich.confidence "
            "(roadmap item 1) — wired into harness behavior by item 8."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description=(
            "Free-form caveats: ambiguities the decomposer couldn't "
            "fully resolve, assumptions made about file layout, etc."
        ),
    )

"""Tests for the parent-issue rollup (D13, 2026-05-17).

Verifies that ``MilestoneCodeDispatcher`` reports completion at the
PARENT-issue granularity (the pre-decomposition feature ids like
``BE-009``) rather than at the unit-of-work granularity
(``BE-009-U3``). Unit-level detail is preserved on the new
``completed_units`` / ``deferred_units`` fields.

Reference: ``docs/backlog/_shipped/decomposer_issue_rollup.md``.
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from bizniz.coder.types import Issue as CoderIssue
from bizniz.driver.milestone_code_dispatcher import _rollup_parent_ids


def _ci(id: str, parent_id: str = None) -> CoderIssue:
    return CoderIssue(
        id=id, title=id, description="x", service="backend",
        parent_issue_id=parent_id,
    )


# ── _rollup_parent_ids ───────────────────────────────────────────


class TestRollupParentIds:
    def test_all_units_pass_parent_completes(self):
        unit_to_parent = {"BE-9-U1": "BE-9", "BE-9-U2": "BE-9",
                          "BE-9-U3": "BE-9"}
        completed = ["BE-9-U1", "BE-9-U2", "BE-9-U3"]
        deferred: List[str] = []
        comp, defer = _rollup_parent_ids(
            unit_to_parent=unit_to_parent,
            completed_units=completed, deferred_units=deferred,
        )
        assert comp == ["BE-9"]
        assert defer == []

    def test_any_unit_fails_parent_deferred(self):
        unit_to_parent = {"BE-9-U1": "BE-9", "BE-9-U2": "BE-9"}
        completed = ["BE-9-U1"]
        deferred = ["BE-9-U2"]
        comp, defer = _rollup_parent_ids(
            unit_to_parent=unit_to_parent,
            completed_units=completed, deferred_units=deferred,
        )
        assert comp == []
        assert defer == ["BE-9"]

    def test_non_decomposed_issue_is_its_own_parent(self):
        """When an issue isn't decomposed, the dispatcher sets
        ``parent_id = coder_issue.id``. Rollup should treat that
        as a 1-unit parent."""
        unit_to_parent = {"BE-7": "BE-7"}  # not decomposed
        comp, defer = _rollup_parent_ids(
            unit_to_parent=unit_to_parent,
            completed_units=["BE-7"], deferred_units=[],
        )
        assert comp == ["BE-7"]
        assert defer == []

    def test_mixed_decomposed_and_non_decomposed(self):
        unit_to_parent = {
            "BE-7": "BE-7",         # not decomposed, passes
            "BE-8": "BE-8",         # not decomposed, fails
            "BE-9-U1": "BE-9",
            "BE-9-U2": "BE-9",      # decomposed, all pass
            "BE-10-U1": "BE-10",
            "BE-10-U2": "BE-10",    # decomposed, partial fail
        }
        completed = ["BE-7", "BE-9-U1", "BE-9-U2", "BE-10-U1"]
        deferred = ["BE-8", "BE-10-U2"]
        comp, defer = _rollup_parent_ids(
            unit_to_parent=unit_to_parent,
            completed_units=completed, deferred_units=deferred,
        )
        # Note: parents-in-encounter order, deduped.
        assert comp == ["BE-7", "BE-9"]
        assert defer == ["BE-8", "BE-10"]

    def test_preserves_first_seen_parent_order(self):
        # Even if all parents land in completed, order is by first
        # unit encounter, not alphabetical.
        unit_to_parent = {
            "C-U1": "C",
            "A-U1": "A",
            "B-U1": "B",
        }
        comp, defer = _rollup_parent_ids(
            unit_to_parent=unit_to_parent,
            completed_units=["C-U1", "A-U1", "B-U1"],
            deferred_units=[],
        )
        assert comp == ["C", "A", "B"]

    def test_empty_inputs(self):
        comp, defer = _rollup_parent_ids(
            unit_to_parent={}, completed_units=[], deferred_units=[],
        )
        assert comp == []
        assert defer == []

    def test_parent_with_single_unit_works_like_non_decomposed(self):
        """Some issues might decompose to 1 unit if the LLM doesn't
        find finer-grained chunks. Rollup should still work."""
        unit_to_parent = {"BE-9-U1": "BE-9"}
        comp, defer = _rollup_parent_ids(
            unit_to_parent=unit_to_parent,
            completed_units=["BE-9-U1"], deferred_units=[],
        )
        assert comp == ["BE-9"]


# ── Integration: _unit_to_coder_issue sets parent_issue_id ────────


class TestUnitToCoderIssueLinkage:
    def test_unit_wrapper_carries_parent_id(self):
        """The shim that wraps a UnitOfWork as a CoderIssue must set
        parent_issue_id so the rollup can attribute it."""
        from bizniz.decomposer.types import UnitOfWork
        from bizniz.driver.milestone_code_dispatcher import (
            _unit_to_coder_issue,
        )

        parent = _ci("BE-9")
        unit = UnitOfWork(
            id="BE-9-U2",
            summary="Implement GET /api/recipes",
            description="Add the route handler",
            kind="new_symbol",
            target_file="app/api/routes/recipes.py",
            expected_test_kind="unit_test",
            depends_on=[],
        )
        wrapped = _unit_to_coder_issue(unit, parent)
        assert wrapped.id == "BE-9-U2"
        assert wrapped.parent_issue_id == "BE-9"

    def test_non_decomposed_issue_has_no_parent_id(self):
        """When the dispatcher receives an Issue not from the
        Decomposer, parent_issue_id stays None — the rollup logic in
        run() treats that as the issue being its own parent."""
        c = _ci("BE-7")
        assert c.parent_issue_id is None

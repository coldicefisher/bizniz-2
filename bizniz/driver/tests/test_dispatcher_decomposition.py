"""Tests for the Decomposer integration in MilestoneCodeDispatcher
(roadmap item 4, part 2/2).

Covers the dispatch-time integration: factory wired vs unwired,
unit-to-issue wrapping, fallback on Decomposer failure, and that
the orchestrator receives the right shape.
"""
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.coder.types import CoderResult, Issue as CoderIssue
from bizniz.decomposer.agent import DecomposerError
from bizniz.decomposer.types import DecompositionResult, UnitOfWork
from bizniz.driver.milestone_code_dispatcher import (
    MilestoneCodeDispatcher,
    _unit_to_coder_issue,
)
from bizniz.lib.model_progression import ModelProgression
from bizniz.quality_engineer.types import EnrichedSpec


def _backend():
    return ServiceDefinition(
        name="backend", service_type="backend", framework="fastapi",
        language="python", description="API",
        workspace_name="backend", port=8000, depends_on=[],
    )


def _arch():
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=[_backend()],
    )


def _spec():
    return EnrichedSpec(milestone_name="M1", capabilities=[])


def _issue(id_, deps=None):
    return CoderIssue(
        id=id_, title=id_, description="parent description",
        service="backend", language="python",
        target_files=[f"{id_}.py"],
        test_files=[],
        success_criteria=[], spec_refs=["c1"],
        depends_on=deps or [],
    )


def _unit(id_, target="f.py", deps=None) -> UnitOfWork:
    return UnitOfWork(
        id=id_, summary=f"summary-{id_}",
        description=f"do {id_}", target_file=target,
        kind="new_symbol", depends_on=deps or [],
        expected_test_kind="unit_test",
    )


def _planner_factory_returning(per_service_issues):
    def factory(service):
        planner = MagicMock()
        planner.plan_service.return_value = per_service_issues.get(service.name, [])
        return planner
    return factory


def _progression_factory():
    def factory(service):
        return ModelProgression(["lite"])
    return factory


def _coder_factory_passing(*expected_ids):
    """Returns CoderResult(passed) in order for the given IDs."""
    coder = MagicMock()
    coder.code_issue.side_effect = [
        CoderResult(issue_id=i, status="passed", summary="")
        for i in expected_ids
    ]

    def factory(model, service):
        return coder
    return factory, coder


# ── _unit_to_coder_issue helper ───────────────────────────────────


class TestUnitToCoderIssueShim:
    def test_preserves_parent_service_and_language(self):
        parent = _issue("BE-005")
        unit = _unit("BE-005-u1", target="app/api/routes/x.py")
        wrapped = _unit_to_coder_issue(unit, parent)
        assert wrapped.service == "backend"
        assert wrapped.language == "python"

    def test_id_from_unit_not_parent(self):
        parent = _issue("BE-005")
        unit = _unit("BE-005-u1")
        wrapped = _unit_to_coder_issue(unit, parent)
        # Resume granularity tracks units, not parent issue.
        assert wrapped.id == "BE-005-u1"

    def test_target_files_single(self):
        parent = _issue("BE-005")
        unit = _unit("u1", target="path/specific.py")
        wrapped = _unit_to_coder_issue(unit, parent)
        assert wrapped.target_files == ["path/specific.py"]

    def test_description_includes_parent_context(self):
        parent = _issue("BE-005")
        unit = _unit("BE-005-u1")
        wrapped = _unit_to_coder_issue(unit, parent)
        assert "Part of parent issue BE-005" in wrapped.description
        assert "This unit only" in wrapped.description
        assert "do BE-005-u1" in wrapped.description

    def test_unit_notes_included(self):
        parent = _issue("BE-005")
        unit = UnitOfWork(
            id="u1", summary="x", description="y",
            target_file="f.py", notes="tricky edge: empty list",
        )
        wrapped = _unit_to_coder_issue(unit, parent)
        assert "tricky edge" in wrapped.description

    def test_success_criteria_for_unit_test_kind(self):
        parent = _issue("BE-005")
        unit = _unit("u1")
        wrapped = _unit_to_coder_issue(unit, parent)
        # 1 line for the "ships" + 1 line for the "passing unit test"
        assert any("passing unit test" in s for s in wrapped.success_criteria)

    def test_success_criteria_skips_test_for_boilerplate(self):
        parent = _issue("BE-005")
        unit = UnitOfWork(
            id="u1", summary="x", description="y",
            target_file="f.py", kind="bundled_boilerplate",
            expected_test_kind="no_test_needed",
        )
        wrapped = _unit_to_coder_issue(unit, parent)
        assert not any("passing unit test" in s for s in wrapped.success_criteria)

    def test_depends_on_from_unit(self):
        parent = _issue("BE-005", deps=["BE-004"])
        unit = _unit("u1", deps=["BE-005-u-prior", "app/models/x.py::X"])
        wrapped = _unit_to_coder_issue(unit, parent)
        # Unit deps replace parent deps — orchestrator's resolver
        # works on the unit-shaped issue's depends_on.
        assert wrapped.depends_on == ["BE-005-u-prior", "app/models/x.py::X"]

    def test_spec_refs_from_parent(self):
        parent = _issue("BE-005")
        unit = _unit("u1")
        wrapped = _unit_to_coder_issue(unit, parent)
        # spec_refs come from parent (the spec capability is the
        # feature, not the unit).
        assert wrapped.spec_refs == ["c1"]


# ── Dispatcher integration ────────────────────────────────────────


class TestDispatcherDecomposition:
    def test_decomposer_factory_none_passes_issues_through(self):
        # When decomposer_factory is None, dispatcher behaves exactly
        # as pre-item-4 — issues are NOT decomposed.
        coder_factory, coder = _coder_factory_passing("BE-001", "BE-002")
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "backend": [_issue("BE-001"), _issue("BE-002")],
            }),
            coder_factory=coder_factory,
            progression_factory=_progression_factory(),
            decomposer_factory=None,  # explicit
        )
        result = dispatcher.run(architecture=_arch(), enriched_spec=_spec())
        assert "BE-001" in result.completed_issue_ids
        assert "BE-002" in result.completed_issue_ids
        # Coder saw exactly the issues ServicePlanner emitted.
        called_with_ids = [
            call.kwargs.get("issue", call.args[0] if call.args else None).id
            for call in coder.code_issue.call_args_list
        ]
        assert called_with_ids == ["BE-001", "BE-002"]

    def test_decomposer_expands_one_issue_to_units(self):
        # ServicePlanner emits 1 issue. Decomposer breaks it into 2
        # units. Coder gets called twice with unit-shaped issues.
        decomposer = MagicMock()
        decomposer.decompose.return_value = DecompositionResult(
            issue_id="BE-001",
            ordered_units=[_unit("BE-001-u1"), _unit("BE-001-u2")],
            confidence=0.9,
        )

        def decomposer_factory(service):
            return decomposer

        coder_factory, coder = _coder_factory_passing(
            "BE-001-u1", "BE-001-u2",
        )
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "backend": [_issue("BE-001")],
            }),
            coder_factory=coder_factory,
            progression_factory=_progression_factory(),
            decomposer_factory=decomposer_factory,
        )
        result = dispatcher.run(architecture=_arch(), enriched_spec=_spec())
        # Both units completed.
        assert "BE-001-u1" in result.completed_issue_ids
        assert "BE-001-u2" in result.completed_issue_ids
        # Original issue id is NOT in completed (it was replaced).
        assert "BE-001" not in result.completed_issue_ids
        # Decomposer was called once.
        decomposer.decompose.assert_called_once()
        # Coder was called twice — once per unit.
        assert coder.code_issue.call_count == 2

    def test_decomposer_failure_falls_back_to_single_issue(self):
        # Decomposer raises → dispatcher dispatches the original issue
        # as-is. Build still progresses.
        decomposer = MagicMock()
        decomposer.decompose.side_effect = DecomposerError("model output broken")

        def decomposer_factory(service):
            return decomposer

        coder_factory, coder = _coder_factory_passing("BE-001")
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "backend": [_issue("BE-001")],
            }),
            coder_factory=coder_factory,
            progression_factory=_progression_factory(),
            decomposer_factory=decomposer_factory,
        )
        result = dispatcher.run(architecture=_arch(), enriched_spec=_spec())
        # Fell back to original issue.
        assert "BE-001" in result.completed_issue_ids
        # Coder got the parent issue, not a unit.
        assert coder.code_issue.call_count == 1

    def test_decomposer_unexpected_exception_also_falls_back(self):
        # Any exception type (not just DecomposerError) → fall back.
        # Defense-in-depth.
        decomposer = MagicMock()
        decomposer.decompose.side_effect = RuntimeError("boom")

        def decomposer_factory(service):
            return decomposer

        coder_factory, _ = _coder_factory_passing("BE-001")
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "backend": [_issue("BE-001")],
            }),
            coder_factory=coder_factory,
            progression_factory=_progression_factory(),
            decomposer_factory=decomposer_factory,
        )
        result = dispatcher.run(architecture=_arch(), enriched_spec=_spec())
        assert "BE-001" in result.completed_issue_ids

    def test_auto_resume_skips_planner_and_decomposer(self):
        """When the issue store has rows for this milestone+service,
        the dispatcher should reuse them — NOT re-run ServicePlanner
        or Decomposer. Filed 2026-05-16 mid-CRM-M5.

        Key assertion: planner + decomposer NOT called. The
        Orchestrator's per-issue skip-already-passed gate is its
        own concern (covered elsewhere); here we mock the resume
        decision so the test stays scoped to dispatcher behavior.
        """
        from bizniz.coder.types import CoderResult
        from bizniz.orchestrator.types import IssueOutcome
        from bizniz.state.issue_store import ResumeBehavior

        # Mock issue_store: returns one prior row + tells orchestrator
        # it's already done.
        store = MagicMock()
        store.all_rows.return_value = [
            {  # shape returned by ProjectDB.list_coder_issues
                "issue_id": "BE-001-U1", "title": "x",
                "description": "y", "service": "backend",
                "language": "python", "target_files": "[]",
                "test_files": "[]", "success_criteria": "[]",
                "spec_refs": "[]", "depends_on": "[]",
                "status": "passed",
            },
        ]
        store.resume_decision.return_value = ResumeBehavior.SKIP
        store.previous_outcome.return_value = IssueOutcome(
            issue_id="BE-001-U1",
            disposition="passed",
            tiers_used=["claude-cli"],
            final_result=CoderResult(
                issue_id="BE-001-U1", status="passed", summary="",
            ),
        )
        store.record_planned = MagicMock()

        # Set up planner that should NOT be called.
        planner = MagicMock()

        def planner_factory(_service):
            return planner

        decomposer = MagicMock()

        def decomposer_factory(_service):
            return decomposer

        # Coder.code_issue should NOT be called (the only unit is
        # already passed). Coder factory may be invoked for instance
        # construction (orchestrator's optimization is shape-dependent),
        # so use a MagicMock and assert on code_issue itself.
        coder_mock = MagicMock()

        def coder_factory(model, service):
            return coder_mock

        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=planner_factory,
            coder_factory=coder_factory,
            progression_factory=_progression_factory(),
            decomposer_factory=decomposer_factory,
            issue_store=store,
        )
        dispatcher.run(architecture=_arch(), enriched_spec=_spec())
        # Planner + decomposer NOT called (the key assertion).
        planner.plan_service.assert_not_called()
        decomposer.decompose.assert_not_called()
        # Coder also didn't run any issue (already passed).
        coder_mock.code_issue.assert_not_called()

    def test_no_prior_rows_runs_planner_normally(self):
        """When the store is empty (fresh build / new milestone), the
        dispatcher should run ServicePlanner + Decomposer as usual.
        Auto-resume only fires when prior rows exist."""
        store = MagicMock()
        store.all_rows.return_value = []  # empty store

        coder_factory, _ = _coder_factory_passing("BE-001-U1")
        decomposer = MagicMock()
        decomposer.decompose.return_value = DecompositionResult(
            issue_id="BE-001",
            ordered_units=[_unit("BE-001-U1")],
            confidence=0.9,
        )

        def decomposer_factory(_service):
            return decomposer

        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "backend": [_issue("BE-001")],
            }),
            coder_factory=coder_factory,
            progression_factory=_progression_factory(),
            decomposer_factory=decomposer_factory,
            issue_store=store,
        )
        dispatcher.run(architecture=_arch(), enriched_spec=_spec())
        # Planner + decomposer DID run.
        decomposer.decompose.assert_called_once()

    def test_unit_ordering_preserves_decomposer_output(self):
        # The flat list of unit-issues maintains the order the
        # Decomposer returned (dependency order).
        decomposer = MagicMock()
        decomposer.decompose.return_value = DecompositionResult(
            issue_id="BE-001",
            ordered_units=[
                _unit("BE-001-u1"),
                _unit("BE-001-u2", deps=["BE-001-u1"]),
                _unit("BE-001-u3", deps=["BE-001-u2"]),
            ],
            confidence=0.95,
        )

        def decomposer_factory(service):
            return decomposer

        coder_factory, coder = _coder_factory_passing(
            "BE-001-u1", "BE-001-u2", "BE-001-u3",
        )
        dispatcher = MilestoneCodeDispatcher(
            service_planner_factory=_planner_factory_returning({
                "backend": [_issue("BE-001")],
            }),
            coder_factory=coder_factory,
            progression_factory=_progression_factory(),
            decomposer_factory=decomposer_factory,
        )
        dispatcher.run(architecture=_arch(), enriched_spec=_spec())
        # Coder saw units in the right order.
        called_ids = [
            call.kwargs.get("issue", call.args[0] if call.args else None).id
            for call in coder.code_issue.call_args_list
        ]
        assert called_ids == ["BE-001-u1", "BE-001-u2", "BE-001-u3"]

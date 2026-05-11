"""Tests for driver.state — JSON-backed sub-phase tracking + resume."""
import json

import pytest

from bizniz.driver.state import (
    MilestoneState, RunState, SubPhase, TopPhase, next_subphase,
)


class TestNextSubphase:
    def test_none_returns_first(self):
        assert next_subphase(None) == SubPhase.ENRICH

    def test_progression(self):
        assert next_subphase(SubPhase.ENRICH) == SubPhase.IMPLEMENT
        assert next_subphase(SubPhase.IMPLEMENT) == SubPhase.SMOKE
        assert next_subphase(SubPhase.SMOKE) == SubPhase.REVIEW_INITIAL
        assert next_subphase(SubPhase.REVIEW_INITIAL) == SubPhase.REPAIR_ITER_0
        assert next_subphase(SubPhase.REPAIR_ITER_0) == SubPhase.REPAIR_ITER_1
        assert next_subphase(SubPhase.REPAIR_ITER_2) == SubPhase.REVIEW_FINAL
        assert next_subphase(SubPhase.REVIEW_FINAL) == SubPhase.INTEGRATION_API
        assert next_subphase(SubPhase.INTEGRATION_API) == SubPhase.INTEGRATION_WORKER
        assert next_subphase(SubPhase.INTEGRATION_WORKER) == SubPhase.INTEGRATION_WEB
        assert next_subphase(SubPhase.INTEGRATION_WEB) == SubPhase.DONE

    def test_done_is_terminal(self):
        assert next_subphase(SubPhase.DONE) == SubPhase.DONE


class TestMilestoneState:
    def test_creates_root(self, tmp_path):
        s = MilestoneState(tmp_path / "m1", milestone_index=1)
        assert (tmp_path / "m1").exists()

    def test_no_phases_initially(self, tmp_path):
        s = MilestoneState(tmp_path / "m1", 1)
        assert s.completed_phases() == []
        assert s.last_completed() is None
        assert s.is_done() is False

    def test_mark_phase_persists(self, tmp_path):
        s = MilestoneState(tmp_path / "m1", 1)
        s.mark_phase(SubPhase.ENRICH, {"capability": "x"})
        # Reload fresh instance — should still see the phase.
        s2 = MilestoneState(tmp_path / "m1", 1)
        assert SubPhase.ENRICH in s2.completed_phases()
        assert s2.last_completed() == SubPhase.ENRICH

    def test_artifact_round_trip(self, tmp_path):
        s = MilestoneState(tmp_path / "m1", 1)
        s.mark_phase(SubPhase.ENRICH, {"key": "value", "list": [1, 2]})
        art = s.read_artifact(SubPhase.ENRICH)
        assert art == {"key": "value", "list": [1, 2]}

    def test_pydantic_model_round_trip(self, tmp_path):
        from bizniz.quality_engineer.types import EnrichedSpec, CapabilitySpec
        spec = EnrichedSpec(
            milestone_name="M1",
            capabilities=[CapabilitySpec(
                id="cap0", name="N", description="d",
                inputs=[], outputs=[], validation_rules=[],
                error_cases=[], edge_cases=[],
                auth_required=True, allowed_roles=[], test_scenarios=[],
            )],
        )
        s = MilestoneState(tmp_path / "m1", 1)
        s.mark_phase(SubPhase.ENRICH, spec)
        art = s.read_artifact(SubPhase.ENRICH)
        assert art["milestone_name"] == "M1"
        assert art["capabilities"][0]["id"] == "cap0"

    def test_mark_phase_idempotent(self, tmp_path):
        s = MilestoneState(tmp_path / "m1", 1)
        s.mark_phase(SubPhase.ENRICH, {"v": 1})
        s.mark_phase(SubPhase.ENRICH, {"v": 2})  # remark replaces
        assert s.completed_phases().count(SubPhase.ENRICH) == 1
        assert s.read_artifact(SubPhase.ENRICH) == {"v": 2}

    def test_last_completed_is_latest_in_order(self, tmp_path):
        # Even if marks come out of order, last_completed returns the
        # latest in declaration order.
        s = MilestoneState(tmp_path / "m1", 1)
        s.mark_phase(SubPhase.IMPLEMENT)
        s.mark_phase(SubPhase.ENRICH)
        assert s.last_completed() == SubPhase.IMPLEMENT

    def test_is_done(self, tmp_path):
        s = MilestoneState(tmp_path / "m1", 1)
        assert s.is_done() is False
        s.mark_phase(SubPhase.DONE)
        assert s.is_done() is True

    def test_corrupt_status_recovers(self, tmp_path):
        s = MilestoneState(tmp_path / "m1", 1)
        s.status_path.write_text("not json")
        # Should treat as no completion, not crash.
        assert s.completed_phases() == []

    def test_unknown_phase_in_status_filtered(self, tmp_path):
        s = MilestoneState(tmp_path / "m1", 1)
        s.status_path.write_text(json.dumps({
            "completed": ["enrich", "phantom_phase", "implement"],
        }))
        completed = s.completed_phases()
        assert SubPhase.ENRICH in completed
        assert SubPhase.IMPLEMENT in completed
        assert len(completed) == 2  # phantom dropped


class TestRunState:
    def test_top_phase_round_trip(self, tmp_path):
        rs = RunState(tmp_path)
        rs.mark_top_phase(TopPhase.PLAN, {"foo": "bar"})
        rs2 = RunState(tmp_path)
        assert rs2.is_top_phase_done(TopPhase.PLAN)
        assert (tmp_path / "plan.json").exists()
        assert json.loads((tmp_path / "plan.json").read_text()) == {"foo": "bar"}

    def test_milestone_factory(self, tmp_path):
        rs = RunState(tmp_path)
        m1 = rs.milestone(1)
        assert m1.root == tmp_path / "m1"
        m2 = rs.milestone(2)
        assert m2.root == tmp_path / "m2"

    def test_first_unfinished_milestone(self, tmp_path):
        rs = RunState(tmp_path)
        # M1 done, M2 partial, M3 untouched
        rs.milestone(1).mark_phase(SubPhase.DONE)
        rs.milestone(2).mark_phase(SubPhase.IMPLEMENT)
        assert rs.first_unfinished_milestone(3) == 2

    def test_first_unfinished_when_all_done(self, tmp_path):
        rs = RunState(tmp_path)
        rs.milestone(1).mark_phase(SubPhase.DONE)
        rs.milestone(2).mark_phase(SubPhase.DONE)
        assert rs.first_unfinished_milestone(2) == 3

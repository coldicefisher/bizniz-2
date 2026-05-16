"""Tests for the confidence-signal load-bearing logic (roadmap item 1).

Covers ``MilestoneLoop._maybe_re_enrich``:

  - High confidence (>= 0.6) → spec returned as-is, no re-enrich.
  - Mid-band (0.4-0.6) → one re-enrich pass, higher of two returned.
  - Low (< 0.4) → soft gate fires; ``auto`` warns + continues,
    ``interactive`` halts.

Doesn't test the agent prompt itself — that's covered indirectly
by the parametrized live runs in roadmap item 8 (Claude perf test).
"""
from unittest.mock import MagicMock, patch

import pytest

from bizniz.driver.gates import GateAction, GatePolicy, GateViolation
from bizniz.driver.milestone_loop import MilestoneLoop
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.types import EnrichedSpec


def _milestone() -> Milestone:
    return Milestone(
        sequence_index=0,
        name="test milestone",
        problem_slice="do the thing",
        use_cases=["user does X"],
        success_criteria=["X is done"],
    )


def _spec(confidence: float, name: str = "test milestone") -> EnrichedSpec:
    """A minimally-valid EnrichedSpec at the given confidence."""
    return EnrichedSpec(
        milestone_name=name,
        capabilities=[{
            "id": "c1",
            "name": "DoX",
            "description": "user can do X",
            "scenarios": [{
                "id": "s1",
                "name": "happy path",
                "description": "given/when/then",
            }],
        }],
        confidence=confidence,
    )


def _make_loop_skeleton(qe_mock: MagicMock, gates: GatePolicy) -> MilestoneLoop:
    """Construct a MilestoneLoop with most dependencies as MagicMock
    — only ``_qe`` and ``_gates`` matter for confidence-signal tests."""
    # MilestoneLoop has many required args; we MagicMock the ones the
    # confidence-signal path doesn't touch.
    loop = MilestoneLoop.__new__(MilestoneLoop)
    loop._qe = qe_mock
    loop._gates = gates
    loop._on_status = None
    loop._confidence_low_threshold = 0.6
    loop._confidence_halt_threshold = 0.4
    return loop


class TestHighConfidenceBypass:
    def test_high_confidence_returns_spec_as_is(self):
        qe = MagicMock()
        qe.re_enrich = MagicMock()  # should NOT be called
        gates = GatePolicy(mode="auto")
        loop = _make_loop_skeleton(qe, gates)

        spec = _spec(0.8)
        out = loop._maybe_re_enrich(
            spec=spec,
            milestone=_milestone(),
            architecture=MagicMock(),
            auth_contract=None,
            prior_list=[],
        )
        assert out is spec
        qe.re_enrich.assert_not_called()

    def test_exactly_at_threshold_is_high_band(self):
        # 0.6 boundary: included in "high band" (>= 0.6).
        qe = MagicMock()
        qe.re_enrich = MagicMock()
        gates = GatePolicy(mode="auto")
        loop = _make_loop_skeleton(qe, gates)

        spec = _spec(0.6)
        out = loop._maybe_re_enrich(
            spec=spec, milestone=_milestone(), architecture=MagicMock(),
            auth_contract=None, prior_list=[],
        )
        assert out is spec
        qe.re_enrich.assert_not_called()


class TestMidBandReEnrich:
    def test_mid_band_triggers_re_enrich(self):
        qe = MagicMock()
        retry = _spec(0.75)
        qe.re_enrich = MagicMock(return_value=retry)
        gates = GatePolicy(mode="auto")
        loop = _make_loop_skeleton(qe, gates)

        spec = _spec(0.5)
        out = loop._maybe_re_enrich(
            spec=spec, milestone=_milestone(), architecture=MagicMock(),
            auth_contract=None, prior_list=[],
        )
        # Retry had higher confidence — returned.
        assert out is retry
        qe.re_enrich.assert_called_once()

    def test_re_enrich_with_lower_confidence_keeps_original(self):
        qe = MagicMock()
        worse_retry = _spec(0.45)
        qe.re_enrich = MagicMock(return_value=worse_retry)
        gates = GatePolicy(mode="auto")
        loop = _make_loop_skeleton(qe, gates)

        spec = _spec(0.55)
        out = loop._maybe_re_enrich(
            spec=spec, milestone=_milestone(), architecture=MagicMock(),
            auth_contract=None, prior_list=[],
        )
        # Original was higher — kept.
        assert out is spec

    def test_re_enrich_raising_falls_back_to_original(self):
        qe = MagicMock()
        qe.re_enrich = MagicMock(side_effect=RuntimeError("API blew up"))
        gates = GatePolicy(mode="auto")
        loop = _make_loop_skeleton(qe, gates)

        spec = _spec(0.5)
        out = loop._maybe_re_enrich(
            spec=spec, milestone=_milestone(), architecture=MagicMock(),
            auth_contract=None, prior_list=[],
        )
        # Defensive: re-enrich exception doesn't tank the pipeline.
        assert out is spec

    def test_just_above_halt_threshold_is_mid_band(self):
        # 0.4 boundary is the halt threshold; 0.41 should re-enrich.
        qe = MagicMock()
        qe.re_enrich = MagicMock(return_value=_spec(0.7))
        gates = GatePolicy(mode="auto")
        loop = _make_loop_skeleton(qe, gates)

        spec = _spec(0.41)
        loop._maybe_re_enrich(
            spec=spec, milestone=_milestone(), architecture=MagicMock(),
            auth_contract=None, prior_list=[],
        )
        qe.re_enrich.assert_called_once()


class TestLowConfidenceGate:
    def test_below_halt_threshold_fires_soft_gate(self):
        qe = MagicMock()
        qe.re_enrich = MagicMock()  # should NOT be called
        gates = GatePolicy(mode="auto")
        loop = _make_loop_skeleton(qe, gates)

        spec = _spec(0.3)
        out = loop._maybe_re_enrich(
            spec=spec, milestone=_milestone(), architecture=MagicMock(),
            auth_contract=None, prior_list=[],
        )
        # ``auto`` warns + returns the low-confidence spec.
        assert out is spec
        qe.re_enrich.assert_not_called()

    def test_below_halt_threshold_halts_in_interactive_mode(self):
        qe = MagicMock()
        qe.re_enrich = MagicMock()
        gates = GatePolicy(mode="interactive")
        loop = _make_loop_skeleton(qe, gates)

        spec = _spec(0.3)
        with pytest.raises(GateViolation) as exc_info:
            loop._maybe_re_enrich(
                spec=spec, milestone=_milestone(), architecture=MagicMock(),
                auth_contract=None, prior_list=[],
            )
        assert exc_info.value.gate_name == "enrich_low_confidence"
        # The gate is soft, but interactive mode raises anyway.
        assert exc_info.value.hard is False

    def test_strict_mode_does_not_halt_on_low_confidence(self):
        # Soft gate is warn-in-strict, halt-in-interactive. strict
        # mode is the default for non-interactive non-auto runs; it
        # warns and continues, same as auto.
        qe = MagicMock()
        qe.re_enrich = MagicMock()
        gates = GatePolicy(mode="strict")
        loop = _make_loop_skeleton(qe, gates)

        spec = _spec(0.3)
        out = loop._maybe_re_enrich(
            spec=spec, milestone=_milestone(), architecture=MagicMock(),
            auth_contract=None, prior_list=[],
        )
        assert out is spec
        qe.re_enrich.assert_not_called()


class TestThresholdCustomization:
    def test_custom_low_threshold_changes_mid_band(self):
        qe = MagicMock()
        qe.re_enrich = MagicMock(return_value=_spec(0.85))
        gates = GatePolicy(mode="auto")
        loop = _make_loop_skeleton(qe, gates)
        # Bump low threshold: now 0.75 triggers re-enrich.
        loop._confidence_low_threshold = 0.8

        spec = _spec(0.75)
        loop._maybe_re_enrich(
            spec=spec, milestone=_milestone(), architecture=MagicMock(),
            auth_contract=None, prior_list=[],
        )
        qe.re_enrich.assert_called_once()

    def test_custom_halt_threshold_changes_gate_trigger(self):
        qe = MagicMock()
        qe.re_enrich = MagicMock()  # should NOT fire
        gates = GatePolicy(mode="interactive")
        loop = _make_loop_skeleton(qe, gates)
        # Bump halt threshold: now 0.55 fires the gate.
        loop._confidence_halt_threshold = 0.6
        loop._confidence_low_threshold = 0.7

        spec = _spec(0.55)
        with pytest.raises(GateViolation):
            loop._maybe_re_enrich(
                spec=spec, milestone=_milestone(),
                architecture=MagicMock(),
                auth_contract=None, prior_list=[],
            )
        qe.re_enrich.assert_not_called()

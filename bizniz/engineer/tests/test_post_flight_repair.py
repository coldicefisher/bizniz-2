"""Tests for post_flight_repair.

The orchestrator is plumbing — verify it calls the debugger correctly,
applies fixes, re-runs the validator, and returns the right outcome.
The debugger itself is mocked; we don't exercise its LLM logic here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bizniz.engineer.post_flight_repair import repair_post_flight_failure
from bizniz.integration.debug_loop import DebuggerTierSpec


def _spec(factory, label="flash-top", attempts=2, turns=12):
    return DebuggerTierSpec(
        factory=factory,
        model_label=label,
        tool_iterations=turns,
        repair_attempts=attempts,
    )


def _make_diagnosis(fixes=None, diagnosis="sync/async mismatch"):
    diag = MagicMock()
    diag.diagnosis = diagnosis
    diag.root_cause_category = "type_error"
    diag.fix_target = "code"
    diag.code_fixes = fixes or []
    return diag


def _make_fix(filepath, content="fixed"):
    fix = MagicMock()
    fix.filepath = filepath
    fix.new_content = content
    return fix


def test_repair_succeeds_first_attempt(tmp_path):
    """Debugger proposes fix, applied, validator re-run passes."""
    workspace = MagicMock()
    workspace.path.return_value = tmp_path

    debugger = MagicMock()
    debugger._tool_iterations = 99
    debugger.diagnose.return_value = _make_diagnosis(
        fixes=[_make_fix("app/api/properties.py", "async def get(...)...")],
    )

    rerun = MagicMock(return_value=(True, ""))

    ok, _ = repair_post_flight_failure(
        service_name="backend",
        workspace=workspace,
        validator_output="app/api/properties.py:32: error: Incompatible await",
        failing_files=["app/api/properties.py"],
        rerun_validator=rerun,
        escalation=[_spec(lambda ws: debugger)],
    )
    assert ok is True
    workspace.write_file.assert_called_once_with(
        path="app/api/properties.py",
        content="async def get(...)...",
    )
    rerun.assert_called_once()
    # tool_iterations was applied from the tier spec
    assert debugger._tool_iterations == 12


def test_repair_escalates_when_first_tier_exhausts(tmp_path):
    workspace = MagicMock()
    workspace.path.return_value = tmp_path
    workspace.write_file = MagicMock()

    flash = MagicMock()
    flash.diagnose.return_value = _make_diagnosis(
        fixes=[_make_fix("a.py", "v1")],
    )
    pro = MagicMock()
    pro.diagnose.return_value = _make_diagnosis(
        fixes=[_make_fix("a.py", "v2")],
    )

    # First call to rerun returns failing, second returns failing,
    # third (under pro) returns passing.
    outcomes = iter([(False, "still bad"), (False, "still bad"), (True, "")])
    rerun = MagicMock(side_effect=lambda: next(outcomes))

    ok, _ = repair_post_flight_failure(
        service_name="backend",
        workspace=workspace,
        validator_output="a.py:1: error: x",
        failing_files=["a.py"],
        rerun_validator=rerun,
        escalation=[
            _spec(lambda ws: flash, label="flash-top", attempts=2),
            _spec(lambda ws: pro, label="pro", attempts=1),
        ],
    )
    assert ok is True
    # flash-top got both attempts before escalating
    assert flash.diagnose.call_count == 2
    assert pro.diagnose.call_count == 1


def test_repair_returns_false_when_chain_exhausted(tmp_path):
    workspace = MagicMock()
    workspace.path.return_value = tmp_path
    workspace.write_file = MagicMock()

    debugger = MagicMock()
    debugger.diagnose.return_value = _make_diagnosis(
        fixes=[_make_fix("a.py", "fixed")],
    )

    rerun = MagicMock(return_value=(False, "still bad"))

    ok, output = repair_post_flight_failure(
        service_name="backend",
        workspace=workspace,
        validator_output="a.py:1: error: x",
        failing_files=["a.py"],
        rerun_validator=rerun,
        escalation=[_spec(lambda ws: debugger, attempts=2)],
    )
    assert ok is False
    # Both attempts ran
    assert debugger.diagnose.call_count == 2


def test_repair_skips_attempt_when_no_fixes_proposed(tmp_path):
    workspace = MagicMock()
    workspace.path.return_value = tmp_path
    workspace.write_file = MagicMock()

    # First diagnosis has no fixes (continue), second has a fix
    debugger = MagicMock()
    debugger.diagnose.side_effect = [
        _make_diagnosis(fixes=[]),
        _make_diagnosis(fixes=[_make_fix("a.py", "v1")]),
    ]

    rerun = MagicMock(return_value=(True, ""))

    ok, _ = repair_post_flight_failure(
        service_name="backend",
        workspace=workspace,
        validator_output="a.py:1: error",
        failing_files=["a.py"],
        rerun_validator=rerun,
        escalation=[_spec(lambda ws: debugger, attempts=2)],
    )
    assert ok is True
    # No fix written for first attempt; second attempt did
    assert workspace.write_file.call_count == 1
    # Validator only re-runs when a fix was actually applied
    assert rerun.call_count == 1


def test_failing_files_inferred_from_validator_output_when_empty(tmp_path):
    """If caller doesn't supply failing_files, we extract them from
    the validator output. mypy formats lines as path:line: error:..."""
    workspace = MagicMock()
    workspace.path.return_value = tmp_path
    workspace.write_file = MagicMock()

    debugger = MagicMock()
    debugger.diagnose.return_value = _make_diagnosis(
        fixes=[_make_fix("app/x.py", "fixed")],
    )

    rerun = MagicMock(return_value=(True, ""))

    repair_post_flight_failure(
        service_name="backend",
        workspace=workspace,
        validator_output=(
            "app/api/properties.py:32: error: A\n"
            "app/api/properties.py:56: error: B\n"
            "app/services/property_service.py:14: error: C\n"
            "Found 3 errors in 2 files"
        ),
        failing_files=[],
        rerun_validator=rerun,
        escalation=[_spec(lambda ws: debugger)],
    )

    # The diagnose call should have received the inferred file list
    args = debugger.diagnose.call_args.kwargs
    assert "app/api/properties.py" in args["source_files"]
    assert "app/services/property_service.py" in args["source_files"]


def test_diagnose_exception_returns_false(tmp_path):
    workspace = MagicMock()
    workspace.path.return_value = tmp_path

    def factory(ws):
        bad = MagicMock()
        bad.diagnose.side_effect = RuntimeError("LLM exploded")
        return bad

    rerun = MagicMock(return_value=(True, ""))

    ok, _ = repair_post_flight_failure(
        service_name="backend",
        workspace=workspace,
        validator_output="x",
        failing_files=["a.py"],
        rerun_validator=rerun,
        escalation=[_spec(factory)],
    )
    assert ok is False
    rerun.assert_not_called()

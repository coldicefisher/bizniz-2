"""Tests for ``FinalTester`` — the end-of-milestone e2e canary."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.driver.final_tester import FinalTester, FinalTestResult
from bizniz.driver.smoke_phase import SmokeCheck, SmokePhaseResult
from bizniz.planner.types import Milestone


def _arch() -> SystemArchitecture:
    return SystemArchitecture(
        project_name="T", project_slug="t", description="",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend",
                framework="fastapi", language="python",
                description="", workspace_name="backend", port=8000,
            ),
        ],
    )


def _milestone() -> Milestone:
    return Milestone(
        sequence_index=0, name="M1", problem_slice="",
    )


def _smoke_result(passed: bool, critical: int = 0) -> SmokePhaseResult:
    return SmokePhaseResult(
        passed=passed,
        checks=[
            SmokeCheck(
                name="health:backend", category="health",
                target="http://localhost:8000/health",
                passed=True, status_code=200, detail="ok",
            ),
        ],
        critical_failures=[
            f"failure {i}" for i in range(critical)
        ],
        duration_s=0.42,
    )


class TestFinalTester:
    def test_delegates_to_smoke_pass(self):
        smoke = MagicMock()
        smoke.run.return_value = _smoke_result(passed=True)
        tester = FinalTester(smoke_phase=smoke)
        result = tester.run(
            milestone=_milestone(),
            architecture=_arch(),
            project_root=Path("/tmp/x"),
            auth_contract="",
        )
        assert isinstance(result, FinalTestResult)
        assert result.passed is True
        assert result.critical_failures == []
        assert len(result.checks) == 1
        # SmokePhase.run was called once with the same arguments.
        assert smoke.run.call_count == 1

    def test_delegates_to_smoke_fail(self):
        smoke = MagicMock()
        smoke.run.return_value = _smoke_result(passed=False, critical=3)
        tester = FinalTester(smoke_phase=smoke)
        result = tester.run(
            milestone=_milestone(),
            architecture=_arch(),
            project_root=Path("/tmp/x"),
            auth_contract="",
        )
        assert result.passed is False
        assert len(result.critical_failures) == 3

    def test_on_status_log_lines_emitted(self):
        smoke = MagicMock()
        smoke.run.return_value = _smoke_result(passed=True)
        statuses: list = []
        tester = FinalTester(
            smoke_phase=smoke,
            on_status=lambda m: statuses.append(m),
        )
        tester.run(
            milestone=_milestone(),
            architecture=_arch(),
            project_root=Path("/tmp/x"),
        )
        joined = " ".join(statuses)
        assert "starting" in joined
        assert "shippable" in joined

    def test_failure_logs_critical_failures(self):
        smoke = MagicMock()
        smoke.run.return_value = _smoke_result(passed=False, critical=2)
        statuses: list = []
        tester = FinalTester(
            smoke_phase=smoke,
            on_status=lambda m: statuses.append(m),
        )
        tester.run(
            milestone=_milestone(),
            architecture=_arch(),
            project_root=Path("/tmp/x"),
        )
        joined = " ".join(statuses)
        assert "NOT shippable" in joined
        # Failures echoed into the log.
        assert "failure 0" in joined

    def test_on_status_exception_is_swallowed(self):
        # A buggy logger callback must not crash the tester.
        smoke = MagicMock()
        smoke.run.return_value = _smoke_result(passed=True)
        def boom(_):
            raise RuntimeError("logger broke")
        tester = FinalTester(smoke_phase=smoke, on_status=boom)
        result = tester.run(
            milestone=_milestone(),
            architecture=_arch(),
            project_root=Path("/tmp/x"),
        )
        assert result.passed is True

    def test_result_carries_duration_from_smoke(self):
        smoke = MagicMock()
        smoke.run.return_value = _smoke_result(passed=True)
        tester = FinalTester(smoke_phase=smoke)
        result = tester.run(
            milestone=_milestone(),
            architecture=_arch(),
            project_root=Path("/tmp/x"),
        )
        assert result.duration_s == 0.42

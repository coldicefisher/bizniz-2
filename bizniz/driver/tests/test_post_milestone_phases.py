"""Tests for UXPhase + RefactorPhase wiring.

Stage 1 of post-milestone phases: structural plumbing only. UXPhase
dispatches to UXDesigner when configured; RefactorPhase is a stub
until the real Refactorer agent ships in Stage 2.
"""
from pathlib import Path
from unittest.mock import MagicMock

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.driver.refactor_phase import RefactorPhase, RefactorPhaseResult
from bizniz.driver.ux_phase import UXPhase, UXPhaseResult
from bizniz.planner.types import Milestone


def _arch(*services):
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=list(services),
    )


def _frontend():
    return ServiceDefinition(
        name="frontend", service_type="frontend", framework="react",
        language="typescript", description="UI",
        workspace_name="frontend", port=5173,
    )


def _backend():
    return ServiceDefinition(
        name="backend", service_type="backend", framework="fastapi",
        language="python", description="API",
        workspace_name="backend", port=8000,
    )


def _milestone(idx=0, refactor_after=False):
    return Milestone(
        sequence_index=idx, name=f"M{idx + 1}", problem_slice="x",
        refactor_after=refactor_after,
    )


class TestUXPhase:
    def test_no_frontend_returns_passed_with_note(self, tmp_path):
        phase = UXPhase(ux_factory=MagicMock())
        result = phase.run(
            milestone=_milestone(),
            architecture=_arch(_backend()),
            project_root=tmp_path,
            service_workspaces={"backend": MagicMock(root=tmp_path)},
            compose_path="/p/c.yml",
        )
        assert result.passed is True
        assert result.services == []
        assert "no frontend" in (result.note or "").lower()

    def test_no_factory_skips(self, tmp_path):
        phase = UXPhase(ux_factory=None)
        result = phase.run(
            milestone=_milestone(),
            architecture=_arch(_frontend()),
            project_root=tmp_path,
            service_workspaces={"frontend": MagicMock(root=tmp_path)},
            compose_path="/p/c.yml",
        )
        assert result.passed is True
        assert result.services == []
        assert "ux_factory" in (result.note or "")

    def test_with_factory_calls_review_frontend(self, tmp_path):
        designer = MagicMock()
        designer.review_frontend.return_value = {
            "initial_score": 6, "final_score": 8,
            "iterations": 2, "fixes_applied": 3, "screenshots_taken": 5,
        }
        phase = UXPhase(ux_factory=lambda _svc: designer)
        ws = MagicMock(root=tmp_path)
        result = phase.run(
            milestone=_milestone(),
            architecture=_arch(_frontend()),
            project_root=tmp_path,
            service_workspaces={"frontend": ws},
            compose_path="/p/c.yml",
            auth_contract="contract",
        )
        designer.review_frontend.assert_called_once()
        assert len(result.services) == 1
        assert result.services[0].service == "frontend"
        assert result.services[0].final_score == 8
        assert result.services[0].fixes_applied == 3

    def test_review_raises_marks_skipped_not_failed(self, tmp_path):
        designer = MagicMock()
        designer.review_frontend.side_effect = RuntimeError("vision down")
        phase = UXPhase(ux_factory=lambda _svc: designer)
        result = phase.run(
            milestone=_milestone(),
            architecture=_arch(_frontend()),
            project_root=tmp_path,
            service_workspaces={"frontend": MagicMock(root=tmp_path)},
            compose_path="/p/c.yml",
        )
        # UX failures don't gate the milestone — they're recorded but
        # the phase still passes so the run continues.
        assert result.passed is True
        assert "RuntimeError" in (result.services[0].skipped_reason or "")


class TestRefactorPhase:
    def test_no_factory_skips(self, tmp_path):
        phase = RefactorPhase(refactorer_factory=None)
        result = phase.run(
            milestone=_milestone(),
            architecture=_arch(_backend()),
            project_root=tmp_path,
            service_workspaces={},
            is_final_milestone=False,
        )
        assert result.passed is True
        assert result.ran is False
        assert result.skipped_reason == "not_implemented"

    def test_factory_runs_and_captures_result(self, tmp_path):
        from bizniz.refactorer.refactorer import RefactorerResult
        refactorer = MagicMock()
        refactorer.run.return_value = RefactorerResult(
            status="no_op", summary="clean", notes=[],
        )
        phase = RefactorPhase(refactorer_factory=lambda: refactorer)
        result = phase.run(
            milestone=_milestone(idx=3),
            architecture=_arch(_backend()),
            project_root=tmp_path,
            service_workspaces={},
            is_final_milestone=True,
        )
        assert result.passed is True
        assert result.ran is True
        assert result.refactorer_result is not None
        assert result.refactorer_result.get("status") == "no_op"

    def test_factory_exception_does_not_gate(self, tmp_path):
        phase = RefactorPhase(
            refactorer_factory=lambda: (_ for _ in ()).throw(
                RuntimeError("boom")
            ),
        )
        result = phase.run(
            milestone=_milestone(),
            architecture=_arch(_backend()),
            project_root=tmp_path,
            service_workspaces={},
            is_final_milestone=True,
        )
        assert result.passed is True
        assert result.ran is False
        assert "RuntimeError" in (result.skipped_reason or "")

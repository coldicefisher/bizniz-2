"""Tests for V2Pipeline ordering + AUTH-skip + FA ID resolvers."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bizniz.auth_agent.types import AuditReport, AuthAgentResult

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.driver.gates import GatePolicy
from bizniz.driver.pipeline import (
    V2Pipeline, V2PipelineResult,
    _has_auth_service, _resolve_fa_app_id, _resolve_fa_tenant_id,
)
from bizniz.driver.state import RunState, TopPhase
from bizniz.planner.types import Milestone, ProjectPlan


# ── Fixtures ────────────────────────────────────────────────────────────


def _arch(*, with_auth: bool):
    services = [
        ServiceDefinition(
            name="backend", service_type="backend", framework="fastapi",
            language="python", description="API",
            workspace_name="backend", port=8000,
        ),
    ]
    if with_auth:
        services.insert(0, ServiceDefinition(
            name="auth", service_type="auth", framework="fusionauth",
            language="yaml", description="Identity",
            workspace_name="fusionauth", port=9011,
        ))
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=services,
    )


def _plan():
    return ProjectPlan(
        project_slug="p", problem_statement="x", description="d",
        milestones=[Milestone(
            sequence_index=1, name="M1", problem_slice="slice",
        )],
    )


def _build_pipeline(tmp_path, *, planner=None, architect=None, auth_factory=None,
                    provisioner=None, ml_loop=None):
    return V2Pipeline(
        planner=planner or MagicMock(),
        architect=architect or MagicMock(),
        auth_agent_factory=auth_factory or MagicMock(return_value=MagicMock()),
        provision_callable=provisioner or MagicMock(),
        milestone_loop=ml_loop or MagicMock(),
        gates=GatePolicy(mode="strict"),
        run_state=RunState(tmp_path / "runs"),
        project_name="p",
        compose_path_for_arch=lambda _a: "/p/c.yml",
    )


# ── Auth detection ─────────────────────────────────────────────────────


class TestHasAuthService:
    def test_true_when_auth_service_present(self):
        assert _has_auth_service(_arch(with_auth=True)) is True

    def test_false_when_no_auth_service(self):
        assert _has_auth_service(_arch(with_auth=False)) is False

    def test_case_insensitive(self):
        arch = SystemArchitecture(
            project_name="P", project_slug="p", description="d",
            services=[ServiceDefinition(
                name="a", service_type="AUTH", framework="fusionauth",
                language="yaml", description="d", workspace_name="a",
                port=9011,
            )],
        )
        assert _has_auth_service(arch) is True


# ── FA ID resolvers ─────────────────────────────────────────────────────


class TestResolveFaIds:
    def test_app_id_from_template_constant(self):
        # Clear env so we hit the template fallback path.
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("FUSIONAUTH_APPLICATION_ID", None)
            uuid = _resolve_fa_app_id()
        assert uuid == "85a03867-dccf-4882-adde-1a79aeec50df"

    def test_tenant_id_from_template_constant(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("FUSIONAUTH_TENANT_ID", None)
            uuid = _resolve_fa_tenant_id()
        assert uuid == "00000000-0000-0000-0000-000000000000"

    def test_env_var_overrides_app_id(self):
        with patch.dict("os.environ", {"FUSIONAUTH_APPLICATION_ID": "override-uuid"}):
            assert _resolve_fa_app_id() == "override-uuid"

    def test_env_var_overrides_tenant_id(self):
        with patch.dict("os.environ", {"FUSIONAUTH_TENANT_ID": "tenant-override"}):
            assert _resolve_fa_tenant_id() == "tenant-override"


# ── Pipeline ordering ───────────────────────────────────────────────────


class TestPipelineOrdering:
    def test_compose_up_runs_before_auth(self, tmp_path):
        """Critical bug fix: AuthAgent needs FA reachable, so compose
        must be up BEFORE auth phase fires.
        """
        call_order: list[str] = []

        planner = MagicMock()
        planner.plan.return_value = _plan()
        architect = MagicMock()
        architect.decompose.return_value = _arch(with_auth=True)

        def _provision(arch, name):
            call_order.append("provision")
            return {"compose_path": "/p/c.yml"}

        def _auth_factory(architecture):
            agent = MagicMock()
            def _configure(**kw):
                call_order.append("auth")
                return AuthAgentResult(
                    mode="configure",
                    contract_markdown="# contract",
                    summary="ok",
                    applied_changes=[],
                    audit=AuditReport(checks=[]),
                )
            agent.configure.side_effect = _configure
            return agent

        ml_loop = MagicMock()

        p = _build_pipeline(
            tmp_path, planner=planner, architect=architect,
            auth_factory=_auth_factory, provisioner=_provision,
            ml_loop=ml_loop,
        )

        # Patch _compose_up to record the call without invoking docker.
        original = p._compose_up
        def _record_compose(_path):
            call_order.append("compose_up")
        p._compose_up = _record_compose

        p.run(problem_statement="x")

        assert call_order == ["provision", "compose_up", "auth"]

    def test_compose_up_still_runs_when_no_auth(self, tmp_path):
        """No auth service → compose_up still runs (milestones need it),
        but auth phase is skipped."""
        call_order: list[str] = []

        planner = MagicMock(); planner.plan.return_value = _plan()
        architect = MagicMock(); architect.decompose.return_value = _arch(with_auth=False)
        def provisioner(a, n):
            call_order.append("provision")
            return {"compose_path": "/p/c.yml"}

        auth_called = []
        def _auth_factory(architecture):
            agent = MagicMock()
            def _configure(**kw):
                auth_called.append(True)
                return MagicMock()
            agent.configure.side_effect = _configure
            return agent

        ml_loop = MagicMock()

        p = _build_pipeline(
            tmp_path, planner=planner, architect=architect,
            auth_factory=_auth_factory, provisioner=provisioner,
            ml_loop=ml_loop,
        )
        p._compose_up = lambda path: call_order.append("compose_up")
        p.run(problem_statement="x")

        assert "compose_up" in call_order
        # AuthAgent.configure should NOT have fired.
        assert auth_called == []

    def test_no_auth_marks_phase_done_with_skipped_sentinel(self, tmp_path):
        planner = MagicMock(); planner.plan.return_value = _plan()
        architect = MagicMock(); architect.decompose.return_value = _arch(with_auth=False)

        def provisioner(a, n):
            return {"compose_path": "/p/c.yml"}

        p = _build_pipeline(
            tmp_path, planner=planner, architect=architect,
            provisioner=provisioner,
            ml_loop=MagicMock(),
        )
        p._compose_up = lambda path: None
        p.run(problem_statement="x")

        # Reload from disk; AUTH phase should be marked done.
        rs = RunState(tmp_path / "runs")
        assert rs.is_top_phase_done(TopPhase.AUTH)
        # Sentinel artifact should mark skipped=True.
        import json
        art = json.loads((rs.root / f"{TopPhase.AUTH.value}.json").read_text())
        assert art.get("skipped") is True
        assert "no auth service" in (art.get("reason") or "")
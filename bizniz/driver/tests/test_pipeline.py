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

# ── AuthAgent audit gate ──────────────────────────────────────────────


from bizniz.auth_agent.types import AuditCheck
from bizniz.driver.gates import GateViolation


class TestAuthAuditGate:
    def _build_p(self, tmp_path, audit_checks):
        planner = MagicMock(); planner.plan.return_value = _plan()
        architect = MagicMock(); architect.decompose.return_value = _arch(with_auth=True)
        provisioner = lambda a, n: {"compose_path": "/p/c.yml"}

        def _auth_factory(architecture):
            agent = MagicMock()
            agent.configure.return_value = AuthAgentResult(
                mode="configure",
                contract_markdown="# contract",
                summary="ok",
                applied_changes=[],
                audit=AuditReport(checks=audit_checks),
            )
            return agent

        ml_loop = MagicMock()
        p = _build_pipeline(
            tmp_path, planner=planner, architect=architect,
            auth_factory=_auth_factory, provisioner=provisioner,
            ml_loop=ml_loop,
        )
        p._compose_up = lambda path: None
        return p, ml_loop

    def test_jwt_signing_failure_halts(self, tmp_path):
        # V2Pipeline.run() catches GateViolation and returns it as a
        # halted_at result; never raises out.
        p, ml_loop = self._build_p(tmp_path, [
            AuditCheck(name="jwt_signing", passed=False,
                       detail="signing key uses 'HS256' — must be RS256"),
        ])
        result = p.run(problem_statement="x")
        assert result.halted_at == "auth_audit_failed"
        assert "jwt_signing" in (result.halt_reason or "")
        # Critical: never proceeded to milestones.
        ml_loop.run.assert_not_called()

    def test_token_validation_failure_halts(self, tmp_path):
        p, ml_loop = self._build_p(tmp_path, [
            AuditCheck(name="token_validation:landlord@example.com",
                       passed=False, detail="login returned 404"),
        ])
        result = p.run(problem_statement="x")
        assert result.halted_at == "auth_audit_failed"
        ml_loop.run.assert_not_called()

    def test_test_users_in_fa_failure_halts(self, tmp_path):
        p, ml_loop = self._build_p(tmp_path, [
            AuditCheck(name="test_users_in_fa", passed=False,
                       detail="contract names users not in FA: x@y.com"),
        ])
        result = p.run(problem_statement="x")
        assert result.halted_at == "auth_audit_failed"
        ml_loop.run.assert_not_called()

    def test_credential_exposure_does_not_halt(self, tmp_path):
        # credential_exposure has known false-positive issues; it's
        # informational, not gating.
        p, ml_loop = self._build_p(tmp_path, [
            AuditCheck(name="credential_exposure", passed=False,
                       detail="found 'password' in legit schema"),
        ])
        p.run(problem_statement="x")
        ml_loop.run.assert_called()

    def test_all_pass_proceeds(self, tmp_path):
        p, ml_loop = self._build_p(tmp_path, [
            AuditCheck(name="jwt_signing", passed=True),
            AuditCheck(name="jwks_reachable", passed=True),
            AuditCheck(name="token_validation:admin@admin.com", passed=True),
        ])
        p.run(problem_statement="x")
        ml_loop.run.assert_called()


class TestPortRemapOnResume:
    """Regression for recipe_box M2 SMOKE: architect.json keeps the
    original ports; provisioner's host-port reassignments live in
    provision.json. On resume the architecture must be patched with
    the remap so SmokePhase probes the right host port."""

    def _seed_run(self, tmp_path):
        import json
        run_dir = tmp_path / "runs" / "j1"
        run_dir.mkdir(parents=True)
        arch = _arch(with_auth=True)
        (run_dir / "architect.json").write_text(arch.model_dump_json())
        (run_dir / "provision.json").write_text(json.dumps({
            "project_name": "p", "project_slug": "p",
            "project_root": str(tmp_path), "compose_path": "x",
            "env_path": "y", "services": [],
            "port_remap": {"backend": [8000, 8012], "auth": [9011, 9024]},
        }))
        (run_dir / "run_status.json").write_text(json.dumps({
            "top_completed": ["plan", "architect", "provision"],
        }))
        return run_dir

    def test_applies_remap_from_provision_json(self, tmp_path):
        from bizniz.driver.state import RunState
        run_dir = self._seed_run(tmp_path)
        state = RunState(run_dir)
        p = V2Pipeline(
            planner=MagicMock(), architect=MagicMock(),
            auth_agent_factory=MagicMock(return_value=MagicMock()),
            provision_callable=MagicMock(),
            milestone_loop=MagicMock(),
            gates=GatePolicy(mode="strict"),
            run_state=state,
            project_name="p",
            compose_path_for_arch=lambda _a: "/p/c.yml",
        )
        loaded = p._top_architect(problem_statement="x", plan=_plan())
        ports = {s.name: s.port for s in loaded.services}
        assert ports["backend"] == 8012
        assert ports["auth"] == 9024

    def test_no_provision_json_leaves_ports_unchanged(self, tmp_path):
        from bizniz.driver.state import RunState
        import json
        run_dir = tmp_path / "runs" / "j2"
        run_dir.mkdir(parents=True)
        (run_dir / "architect.json").write_text(_arch(with_auth=False).model_dump_json())
        (run_dir / "run_status.json").write_text(json.dumps({
            "top_completed": ["plan", "architect"],
        }))
        state = RunState(run_dir)
        p = V2Pipeline(
            planner=MagicMock(), architect=MagicMock(),
            auth_agent_factory=MagicMock(return_value=MagicMock()),
            provision_callable=MagicMock(),
            milestone_loop=MagicMock(),
            gates=GatePolicy(mode="strict"),
            run_state=state,
            project_name="p",
            compose_path_for_arch=lambda _a: "/p/c.yml",
        )
        loaded = p._top_architect(problem_statement="x", plan=_plan())
        ports = {s.name: s.port for s in loaded.services}
        assert ports["backend"] == 8000  # unchanged

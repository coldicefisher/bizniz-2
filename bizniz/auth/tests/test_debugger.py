"""Tests for the FusionAuth debugger (repair_fusionauth_state).

The debugger is deterministic by design — typed fixes mapped from
failed-check names. We mock the orchestrator and validate the call
sequence rather than running against a live FusionAuth.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bizniz.auth.contract import (
    AuthContract,
    ContractRole,
    ContractTestUser,
    ContractEndpoint,
    ContractValidationResult,
    JwtClaimContract,
    RuntimeContract,
    ValidationCheck,
)
from bizniz.auth.debugger import _apply_typed_fixes, repair_fusionauth_state
from bizniz.auth.spec import AuthSpec, RoleSpec, AppSpec, UserSpec


def _make_contract(app_id: str = "app-1") -> AuthContract:
    return AuthContract(
        project_name="test",
        application_id=app_id,
        application_name="Test App",
        fusionauth_url="http://fa",
        fusionauth_public_url="http://fa",
        tenancy_model="roles",
        roles=[ContractRole(name="admin", description="", is_default=False)],
        test_users=[ContractTestUser(email="alice@x", password="pw", roles=["admin"])],
        skeleton_endpoints=[],
        fusionauth_endpoints=[],
        runtime=RuntimeContract(
            jwks_url="http://fa/.well-known/jwks.json",
            issuer="http://fa",
            audience="app-1",
            algorithm="RS256",
            jwt_claims=JwtClaimContract(),
        ),
        frontend_port=5173,
    )


def _make_spec() -> AuthSpec:
    return AuthSpec(
        enabled=True,
        roles=[RoleSpec(name="admin"), RoleSpec(name="user")],
        applications=[AppSpec(name="Test App")],
        test_users=[
            UserSpec(email="alice@x", password="pw", role_names=["admin"]),
        ],
    )


def test_typed_fix_for_user_exists_recreates_user():
    spec = _make_spec()
    contract = _make_contract()
    orch = MagicMock()

    failed = [ValidationCheck("user_exists:alice@x", False, "not found")]
    actions = _apply_typed_fixes(
        failed_checks=failed,
        auth_spec=spec,
        auth_contract=contract,
        orchestrator=orch,
        application_id="app-1",
    )
    assert actions == 1
    orch.ensure_user.assert_called_once()
    args = orch.ensure_user.call_args.kwargs
    assert args["email"] == "alice@x"
    assert args["roles"] == ["admin"]


def test_typed_fix_for_role_exists_recreates_role():
    spec = _make_spec()
    contract = _make_contract()
    orch = MagicMock()

    failed = [ValidationCheck("role_exists:user@Test App", False, "missing")]
    # Note: typed fixer parses just the role name from the check name.
    # If the format is different it gracefully skips.
    actions = _apply_typed_fixes(
        failed_checks=[ValidationCheck("role_exists:user", False, "missing")],
        auth_spec=spec,
        auth_contract=contract,
        orchestrator=orch,
        application_id="app-1",
    )
    assert actions == 1
    orch.ensure_role.assert_called_once()
    assert orch.ensure_role.call_args.kwargs["name"] == "user"


def test_typed_fix_for_application_exists_recreates_app():
    spec = _make_spec()
    contract = _make_contract()
    orch = MagicMock()

    failed = [ValidationCheck("application_exists", False, "missing")]
    actions = _apply_typed_fixes(
        failed_checks=failed,
        auth_spec=spec,
        auth_contract=contract,
        orchestrator=orch,
        application_id="app-1",
    )
    assert actions == 1
    orch.ensure_application.assert_called_once_with(
        app_id="app-1", name="Test App",
    )


def test_typed_fix_skips_unknown_checks():
    spec = _make_spec()
    contract = _make_contract()
    orch = MagicMock()

    failed = [ValidationCheck("jwks_reachable", False, "timeout")]
    actions = _apply_typed_fixes(
        failed_checks=failed,
        auth_spec=spec,
        auth_contract=contract,
        orchestrator=orch,
        application_id="app-1",
    )
    assert actions == 0
    orch.ensure_user.assert_not_called()
    orch.ensure_role.assert_not_called()
    orch.ensure_application.assert_not_called()


def test_repair_returns_ok_when_revalidation_passes(monkeypatch):
    """First iteration's re-materialize + typed fixes succeed; second
    validation pass returns ok=True. Debugger short-circuits."""
    spec = _make_spec()
    contract = _make_contract()
    orch = MagicMock()
    orch.materialize.return_value = MagicMock(
        actions=[MagicMock(applied=True, error=None)],
    )

    # Counter so initial result is failing, post-repair is ok.
    call_count = {"n": 0}

    def fake_validate(orchestrator):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ContractValidationResult(
                ok=False,
                checks=[ValidationCheck("user_exists:alice@x", False, "not found")],
            )
        return ContractValidationResult(
            ok=True,
            checks=[ValidationCheck("user_exists:alice@x", True)],
        )

    monkeypatch.setattr(contract, "validate", fake_validate)

    initial = ContractValidationResult(
        ok=False,
        checks=[ValidationCheck("user_exists:alice@x", False, "not found")],
    )
    result = repair_fusionauth_state(
        auth_spec=spec,
        auth_contract=contract,
        validation_result=initial,
        orchestrator=orch,
        application_id="app-1",
        project_root=__import__("pathlib").Path("/tmp/nonexistent"),
        compose_path="/tmp/nope.yml",
        max_iterations=3,
    )
    assert result.ok is True


def test_repair_gives_up_after_max_iterations(monkeypatch):
    spec = _make_spec()
    contract = _make_contract()
    orch = MagicMock()
    orch.materialize.return_value = MagicMock(
        actions=[MagicMock(applied=True, error=None)],
    )
    orch.wait_until_ready = MagicMock(return_value=False)

    def always_failing(orchestrator):
        return ContractValidationResult(
            ok=False,
            checks=[ValidationCheck("user_roles:alice@x", False, "missing roles")],
        )

    monkeypatch.setattr(contract, "validate", always_failing)

    # Avoid the actual restart subprocess on the last iteration
    import bizniz.auth.debugger as dbg
    monkeypatch.setattr(dbg, "_restart_fusionauth", lambda *a, **kw: True)

    initial = ContractValidationResult(
        ok=False,
        checks=[ValidationCheck("user_roles:alice@x", False, "missing roles")],
    )
    result = repair_fusionauth_state(
        auth_spec=spec,
        auth_contract=contract,
        validation_result=initial,
        orchestrator=orch,
        application_id="app-1",
        project_root=__import__("pathlib").Path("/tmp/nonexistent"),
        compose_path="/tmp/nope.yml",
        max_iterations=2,
    )
    assert result.ok is False
    assert len(result.failed_checks) == 1

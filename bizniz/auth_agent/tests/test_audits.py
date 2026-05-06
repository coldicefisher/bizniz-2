"""Tests for the AuthAgent's deterministic audit battery."""
import base64
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import SystemArchitecture
from bizniz.auth.fusionauth_orchestrator import FusionAuthOrchestrator
from bizniz.auth.types import FusionAuthError
from bizniz.auth_agent.audits import (
    _parse_test_users,
    _parse_issuer,
    audit_credential_exposure,
    audit_jwks_reachable,
    audit_jwt_signing,
    audit_test_users_in_fa,
    audit_token_validation,
    run_audit_battery,
)
from bizniz.auth_agent.types import AuditCheck
from bizniz.workspace.local_workspace import LocalWorkspace


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_jwt(header: dict, payload: dict) -> str:
    def b64(d: dict) -> str:
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{b64(header)}.{b64(payload)}.fake-sig"


def _arch():
    return SystemArchitecture(
        project_name="X",
        project_slug="x",
        description="x",
        services=[],
    )


# ── Contract parsing ─────────────────────────────────────────────────────


class TestParseTestUsers:
    def test_parses_simple_format(self):
        md = (
            "## Test users\n"
            "- admin@admin.com / ChangeMe123! — role super_admin\n"
            "- landlord@example.com / Pass123! — role landlord\n"
        )
        out = _parse_test_users(md)
        assert len(out) == 2
        assert out[0] == ("admin@admin.com", "ChangeMe123!", ["super_admin"])
        assert out[1] == ("landlord@example.com", "Pass123!", ["landlord"])

    def test_parses_multiple_roles(self):
        md = "- admin@a.com / pw — roles super_admin, landlord\n"
        out = _parse_test_users(md)
        assert out == [("admin@a.com", "pw", ["super_admin", "landlord"])]

    def test_returns_empty_on_unparseable(self):
        out = _parse_test_users("just some random text\n")
        assert out == []

    def test_handles_em_dash_and_hyphen_separators(self):
        md = (
            "- a@a.com / pw - role a\n"
            "- b@b.com / pw -- role b\n"
            "- c@c.com / pw — role c\n"
        )
        out = _parse_test_users(md)
        emails = [u[0] for u in out]
        assert "a@a.com" in emails
        assert "c@c.com" in emails


class TestParseIssuer:
    def test_extracts_issuer(self):
        md = "## Tokens\n- Issuer (iss claim): acme.com\n"
        assert _parse_issuer(md) == "acme.com"

    def test_handles_backticks(self):
        md = "Issuer: `http://auth:9011`"
        assert _parse_issuer(md) == "http://auth:9011"

    def test_returns_none_on_missing(self):
        assert _parse_issuer("no issuer here") is None


# ── audit_jwks_reachable ─────────────────────────────────────────────────


class TestJwksReachable:
    def test_pass_with_keys(self):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_jwks.return_value = {"keys": [{"kid": "abc", "alg": "RS256"}]}
        check = audit_jwks_reachable(orch)
        assert check.passed
        assert "abc" in check.detail

    def test_fail_when_endpoint_raises(self):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_jwks.side_effect = FusionAuthError("connection refused")
        check = audit_jwks_reachable(orch)
        assert not check.passed
        assert "connection refused" in check.detail

    def test_fail_when_no_keys(self):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_jwks.return_value = {"keys": []}
        check = audit_jwks_reachable(orch)
        assert not check.passed
        assert "no keys" in check.detail.lower()


# ── audit_jwt_signing ────────────────────────────────────────────────────


class TestJwtSigning:
    def _orch_with_tenant(self, *, key_id="key-1", alg="RS256"):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_tenant.return_value = {
            "tenant": {
                "jwtConfiguration": {"accessTokenKeyId": key_id},
            },
        }
        orch.get_signing_key = MagicMock(return_value={"id": key_id, "algorithm": alg})
        orch.list_signing_keys.return_value = [{"id": key_id, "algorithm": alg}]
        return orch

    def test_pass_rs256(self):
        orch = self._orch_with_tenant(alg="RS256")
        check = audit_jwt_signing(orch, "tenant-1")
        assert check.passed
        assert "RS256" in check.detail

    def test_fail_hs256(self):
        orch = self._orch_with_tenant(alg="HS256")
        check = audit_jwt_signing(orch, "tenant-1")
        assert not check.passed
        assert "HS256" in check.detail or "RS256" in check.detail

    def test_fail_no_signing_key_bound(self):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_tenant.return_value = {"tenant": {"jwtConfiguration": {}}}
        check = audit_jwt_signing(orch, "tenant-1")
        assert not check.passed
        assert "accessTokenKeyId" in check.detail

    def test_fail_tenant_not_found(self):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_tenant.return_value = None
        check = audit_jwt_signing(orch, "tenant-1")
        assert not check.passed
        assert "not found" in check.detail.lower()


# ── audit_token_validation ───────────────────────────────────────────────


class TestTokenValidation:
    def _orch_returning(self, token):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_token.return_value = token
        return orch

    def test_pass_when_claims_match(self):
        token = _make_jwt(
            {"alg": "RS256", "kid": "k"},
            {"iss": "acme.com", "aud": "app-1", "sub": "u1", "roles": ["landlord"]},
        )
        orch = self._orch_returning(token)
        checks = audit_token_validation(
            orch, "app-1",
            test_users=[("landlord@example.com", "pw", ["landlord"])],
            declared_issuer="acme.com",
        )
        assert len(checks) == 1
        assert checks[0].passed
        assert "landlord" in checks[0].detail

    def test_fail_when_login_fails(self):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_token.side_effect = FusionAuthError("invalid creds")
        checks = audit_token_validation(
            orch, "app-1",
            test_users=[("user@example.com", "pw", ["x"])],
        )
        assert not checks[0].passed
        assert "invalid creds" in checks[0].detail

    def test_fail_when_role_missing(self):
        token = _make_jwt(
            {"alg": "RS256"},
            {"iss": "acme.com", "aud": "app-1", "sub": "u1", "roles": ["tenant"]},
        )
        orch = self._orch_returning(token)
        checks = audit_token_validation(
            orch, "app-1",
            test_users=[("u@example.com", "pw", ["landlord"])],
        )
        assert not checks[0].passed
        assert "landlord" in checks[0].detail

    def test_fail_when_iss_drifts_from_contract(self):
        token = _make_jwt(
            {"alg": "RS256"},
            {"iss": "acme.com", "aud": "a", "sub": "u", "roles": []},
        )
        orch = self._orch_returning(token)
        checks = audit_token_validation(
            orch, "app-1",
            test_users=[("u@example.com", "pw", [])],
            declared_issuer="http://auth:9011",
        )
        assert not checks[0].passed
        assert "iss" in checks[0].detail.lower()

    def test_fail_when_alg_is_hs(self):
        token = _make_jwt(
            {"alg": "HS256"},
            {"iss": "x", "aud": "a", "sub": "u", "roles": []},
        )
        orch = self._orch_returning(token)
        checks = audit_token_validation(
            orch, "app-1",
            test_users=[("u@example.com", "pw", [])],
        )
        assert not checks[0].passed
        assert "HS256" in checks[0].detail


# ── audit_credential_exposure ────────────────────────────────────────────


class TestCredentialExposure:
    def test_pass_when_password_only_in_test_files(self, tmp_path):
        ws = LocalWorkspace(root=tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_login.py").write_text(
            'def test_login():\n    pw = "ChangeMe123!"\n'
        )
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "main.py").write_text("# clean\n")
        check = audit_credential_exposure(
            ws, [("admin@admin.com", "ChangeMe123!", ["super_admin"])],
        )
        assert check.passed

    def test_fail_when_password_in_production_code(self, tmp_path):
        ws = LocalWorkspace(root=tmp_path)
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "auth.py").write_text(
            'ADMIN_PASSWORD = "ChangeMe123!"\n'
        )
        check = audit_credential_exposure(
            ws, [("admin@admin.com", "ChangeMe123!", ["super_admin"])],
        )
        assert not check.passed
        assert "auth.py" in check.detail

    def test_skips_when_no_test_users(self, tmp_path):
        ws = LocalWorkspace(root=tmp_path)
        check = audit_credential_exposure(ws, [])
        assert check.passed
        assert "skipped" in check.detail.lower()


# ── audit_test_users_in_fa ───────────────────────────────────────────────


class TestUsersInFA:
    def test_pass_when_all_present(self):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_user_by_email.return_value = {"id": "x"}
        check = audit_test_users_in_fa(
            orch, "app-1",
            test_users=[("a@a.com", "pw", []), ("b@b.com", "pw", [])],
        )
        assert check.passed
        assert "2" in check.detail

    def test_fail_when_missing(self):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_user_by_email.side_effect = lambda e: {"id": "x"} if e == "a@a.com" else None
        check = audit_test_users_in_fa(
            orch, "app-1",
            test_users=[("a@a.com", "pw", []), ("b@b.com", "pw", [])],
        )
        assert not check.passed
        assert "b@b.com" in check.detail


# ── run_audit_battery ────────────────────────────────────────────────────


class TestRunAuditBattery:
    def test_returns_report_with_per_check_results(self, tmp_path):
        token = _make_jwt(
            {"alg": "RS256", "kid": "k"},
            {"iss": "acme.com", "aud": "app-1", "sub": "u1", "roles": ["landlord"]},
        )
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_jwks.return_value = {"keys": [{"kid": "k", "alg": "RS256"}]}
        orch.get_tenant.return_value = {
            "tenant": {"jwtConfiguration": {"accessTokenKeyId": "key-1"}},
        }
        orch.get_signing_key = MagicMock(return_value={"id": "key-1", "algorithm": "RS256"})
        orch.list_signing_keys.return_value = [{"id": "key-1", "algorithm": "RS256"}]
        orch.get_token.return_value = token
        orch.get_user_by_email.return_value = {"id": "u1"}

        ws = LocalWorkspace(root=tmp_path)
        contract = (
            "# Auth Contract\n"
            "## Tokens\n- Issuer (iss claim): acme.com\n"
            "## Test users\n"
            "- landlord@example.com / Pass123! — role landlord\n"
        )
        report = run_audit_battery(
            orch=orch,
            workspace=ws,
            architecture=_arch(),
            primary_app_id="app-1",
            tenant_id="tenant-1",
            contract_markdown=contract,
        )
        assert report.passed
        assert len(report.checks) >= 5  # jwks, signing, parseable, validation, in_fa, exposure
        names = [c.name for c in report.checks]
        assert "jwks_reachable" in names
        assert "jwt_signing" in names
        assert any("token_validation" in n for n in names)
        assert "test_users_in_fa" in names
        assert "credential_exposure" in names

    def test_unparseable_contract_yields_skipped_user_audits(self, tmp_path):
        orch = MagicMock(spec=FusionAuthOrchestrator)
        orch.get_jwks.return_value = {"keys": [{"kid": "k"}]}
        orch.get_tenant.return_value = {
            "tenant": {"jwtConfiguration": {"accessTokenKeyId": "key-1"}},
        }
        orch.get_signing_key = MagicMock(return_value={"id": "key-1", "algorithm": "RS256"})
        orch.list_signing_keys.return_value = [{"id": "key-1", "algorithm": "RS256"}]
        ws = LocalWorkspace(root=tmp_path)
        report = run_audit_battery(
            orch=orch,
            workspace=ws,
            architecture=_arch(),
            primary_app_id="app-1",
            tenant_id="tenant-1",
            contract_markdown="# Just a heading, no test users",
        )
        names = [c.name for c in report.checks]
        # User-related audits absent; only the parseable check fired
        assert "test_users_parseable" in names
        parseable = next(c for c in report.checks if c.name == "test_users_parseable")
        assert not parseable.passed
        assert not any("token_validation" in n for n in names)
        assert "test_users_in_fa" not in names

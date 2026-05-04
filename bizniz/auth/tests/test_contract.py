"""Tests for AuthContract — typed contract + validate() against
mocked FusionAuthOrchestrator."""
import json
from pathlib import Path
from unittest.mock import MagicMock

from bizniz.auth import (
    AuthContract,
    ContractRole,
    ContractTestUser,
    ContractEndpoint,
    JwtClaimContract,
    RuntimeContract,
    FusionAuthError,
    FusionAuthRole,
)


def _make_contract():
    return AuthContract(
        project_name="property_manager_v1",
        application_id="app-uuid-123",
        application_name="Property Manager",
        fusionauth_url="http://fusionauth:9011",
        fusionauth_public_url="http://localhost:9011",
        tenancy_model="roles",
        roles=[
            ContractRole("landlord", "Manages properties"),
            ContractRole("tenant", "Renter", is_default=True),
        ],
        test_users=[
            ContractTestUser(
                "landlord@example.com", "TestPass123!", roles=["landlord"],
            ),
            ContractTestUser(
                "tenant@example.com", "TestPass123!", roles=["tenant"],
            ),
        ],
        skeleton_endpoints=[
            ContractEndpoint("POST", "/api/v1/auth/login", "Login"),
            ContractEndpoint(
                "GET", "/api/v1/auth/me", "Current user", auth_required=True,
            ),
        ],
        runtime=RuntimeContract(
            jwks_url="http://fusionauth:9011/.well-known/jwks.json",
            issuer="http://fusionauth:9011",
            audience="app-uuid-123",
            jwt_claims=JwtClaimContract(),
        ),
    )


# ── Markdown rendering ────────────────────────────────────────────


def test_to_markdown_includes_application_id():
    c = _make_contract()
    md = c.to_markdown()
    assert "app-uuid-123" in md
    assert "Property Manager" in md


def test_to_markdown_lists_test_users_verbatim():
    """Tests rely on these credentials — they MUST appear in the
    markdown so the AI tester reads them."""
    c = _make_contract()
    md = c.to_markdown()
    assert "landlord@example.com" in md
    assert "TestPass123!" in md
    assert "tenant@example.com" in md


def test_to_markdown_includes_runtime_contract():
    """Engineer's get_current_user reads the JWT claim names from
    here. They MUST be in the rendered markdown."""
    c = _make_contract()
    md = c.to_markdown()
    assert "JWKS endpoint" in md
    assert "/.well-known/jwks.json" in md
    assert "RS256" in md
    assert "subject_claim" not in md  # internal field name shouldn't leak
    assert "user_id" in md  # but the semantic should be clear


def test_to_markdown_warns_against_local_jwt_minting():
    c = _make_contract()
    md = c.to_markdown()
    assert "NEVER mints" in md or "MUST NOT mint" in md


# ── Validation ────────────────────────────────────────────────────


def _mock_orchestrator(*, app_exists=True, roles=None, users=None,
                       login_ok=True, userinfo_roles=None):
    """Build a MagicMock that matches the Orchestrator's contract."""
    orch = MagicMock()
    orch.get_application.return_value = (
        {"application": {"id": "app-uuid-123"}} if app_exists else None
    )
    role_objs = {
        name: FusionAuthRole(role_id=f"r-{name}", name=name)
        for name in (roles or [])
    }
    orch.get_role.side_effect = lambda app, name: role_objs.get(name)

    def _get_user(email):
        if users and email in users:
            return {"user": {"id": f"u-{email}"}}
        return None
    orch.get_user_by_email.side_effect = _get_user

    if login_ok:
        orch.get_token.return_value = "fake-jwt"
    else:
        orch.get_token.side_effect = FusionAuthError(
            "Invalid credentials", status_code=401,
        )
    orch.get_user_info.return_value = {
        "roles": userinfo_roles or [],
    }
    return orch


def test_validate_passes_when_everything_matches(monkeypatch):
    c = _make_contract()
    orch = _mock_orchestrator(
        app_exists=True,
        roles=["landlord", "tenant"],
        users={"landlord@example.com", "tenant@example.com"},
        login_ok=True,
        userinfo_roles=["landlord"],  # used for both login attempts
    )
    # Stub out the JWKS HTTP fetch
    import bizniz.auth.contract as contract_mod

    class _MockResp:
        status_code = 200
        def json(self):
            return {"keys": [{"kid": "abc"}]}

    monkeypatch.setattr(
        contract_mod, "requests",
        MagicMock(get=MagicMock(return_value=_MockResp())),
    )

    result = c.validate(orch)
    # We stubbed userinfo to return ["landlord"] for both users; the
    # tenant test will fail role check. So validation will fail —
    # but specifically on the user_roles check, not on existence.
    by_name = {ck.name: ck for ck in result.checks}
    assert by_name["application_exists"].passed
    assert by_name["role_exists:landlord"].passed
    assert by_name["role_exists:tenant"].passed
    assert by_name["user_exists:landlord@example.com"].passed
    assert by_name["user_login:landlord@example.com"].passed


def test_validate_fails_when_role_missing():
    c = _make_contract()
    orch = _mock_orchestrator(
        app_exists=True,
        roles=["landlord"],  # tenant role NOT created
        users={"landlord@example.com", "tenant@example.com"},
    )
    # Skip jwks check by removing runtime
    c.runtime = None

    result = c.validate(orch)
    assert not result.ok
    failed = [c.name for c in result.failed_checks]
    assert "role_exists:tenant" in failed


def test_validate_fails_when_user_missing():
    c = _make_contract()
    orch = _mock_orchestrator(
        app_exists=True,
        roles=["landlord", "tenant"],
        users={"landlord@example.com"},  # tenant user NOT created
    )
    c.runtime = None

    result = c.validate(orch)
    assert not result.ok
    failed = [c.name for c in result.failed_checks]
    assert "user_exists:tenant@example.com" in failed


def test_validate_fails_when_user_cannot_login():
    c = _make_contract()
    orch = _mock_orchestrator(
        app_exists=True,
        roles=["landlord", "tenant"],
        users={"landlord@example.com", "tenant@example.com"},
        login_ok=False,
    )
    c.runtime = None

    result = c.validate(orch)
    assert not result.ok
    failed = [c.name for c in result.failed_checks]
    assert any(name.startswith("user_login:") for name in failed)


def test_validate_records_timestamp():
    c = _make_contract()
    c.runtime = None
    orch = _mock_orchestrator()
    c.validate(orch)
    assert c.validated_at  # ISO format string
    assert c.validation_passed is False  # nothing's seeded in mock


# ── Disk write ────────────────────────────────────────────────────


def test_write_to_creates_md_and_json(tmp_path):
    c = _make_contract()
    c.write_to(tmp_path)
    md_path = tmp_path / "AUTH_CONTRACT.md"
    json_path = tmp_path / "docs" / "auth" / "contract.json"
    assert md_path.exists()
    assert json_path.exists()

    md = md_path.read_text()
    assert "Property Manager" in md

    parsed = json.loads(json_path.read_text())
    assert parsed["application_id"] == "app-uuid-123"
    assert len(parsed["roles"]) == 2
    assert parsed["test_users"][0]["email"] == "landlord@example.com"

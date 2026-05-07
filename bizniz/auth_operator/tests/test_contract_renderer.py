"""Tests for the AUTH_CONTRACT.md renderer."""
from bizniz.auth_operator.contract_renderer import render_auth_contract
from bizniz.auth_operator.manifest import (
    ApplicationManifest, AuthManifest, RoleManifest,
    SigningKeyInfo, UserManifest,
)


def _full_manifest(*, alg="RS256", login_ok=True):
    return AuthManifest(
        fa_url="http://auth:9011",
        primary_app_id="app-1",
        tenant_id="tenant-1",
        issuer="http://auth:9011",
        signing_key=SigningKeyInfo(
            key_id="key-1", algorithm=alg,
            bound_to_app=True, bound_to_tenant=False,
        ),
        applications=[ApplicationManifest(
            name="primary", application_id="app-1",
            role_names=["super_admin", "landlord", "tenant"],
        )],
        roles=[
            RoleManifest(name="super_admin", description="Platform admin", is_super_role=True),
            RoleManifest(name="landlord", description="Property owner"),
            RoleManifest(name="tenant", description="Property occupant"),
        ],
        users=[
            UserManifest(
                email="landlord@example.com", user_id="u-1",
                password="password", first_name="Landlord", last_name="User",
                roles=["landlord"], registered=True, login_verified=login_ok,
            ),
        ],
    )


class TestRender:
    def test_includes_coordinates(self):
        out = render_auth_contract(_full_manifest())
        assert "http://auth:9011" in out
        assert "app-1" in out
        assert "tenant-1" in out

    def test_signing_section(self):
        out = render_auth_contract(_full_manifest(alg="RS256"))
        assert "RS256" in out
        assert "RS-family ✓" in out

    def test_signing_section_flags_hs256(self):
        out = render_auth_contract(_full_manifest(alg="HS256"))
        assert "HS256" in out
        assert "NOT RS-family ✗" in out

    def test_users_section_has_audit_parseable_format(self):
        # Format must match _parse_test_users in audits.py:
        # "- email / password — roles role_name"
        out = render_auth_contract(_full_manifest())
        assert "- landlord@example.com / password — roles landlord" in out

    def test_unverified_users_marked(self):
        out = render_auth_contract(_full_manifest(login_ok=False))
        assert "login unverified" in out

    def test_roles_with_super_marker(self):
        out = render_auth_contract(_full_manifest())
        assert "[super_role]" in out  # super_admin marker

    def test_no_users_renders_cleanly(self):
        manifest = _full_manifest()
        manifest.users = []
        out = render_auth_contract(manifest)
        assert "## Test users" not in out
        assert "## JWT signing" in out

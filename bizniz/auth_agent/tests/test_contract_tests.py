"""Tests for the deterministic auth-contract test renderer.

We compile the rendered file via ``compile()`` to make sure it's
valid Python, and we assert specific test functions exist for the
users the contract names. Runtime behavior (httpx + FA) is exercised
end-to-end during the integration phase, not here.
"""
import re

import pytest

from bizniz.auth_agent.contract_tests import (
    _slugify,
    render_auth_contract_test_file,
)


_CONTRACT_FULL = """\
# Auth Contract — Property Manager M1

## Issuer

- Issuer (iss claim): https://auth.example.com

## Test users

- admin@admin.com / password — role super_admin
- landlord@example.com / password — roles landlord, manager
- tenant@example.com / password — role tenant
"""


_CONTRACT_NO_USERS = """\
# Auth Contract — Empty

(no test users this milestone)
"""


_CONTRACT_NO_ISSUER = """\
## Test users

- admin@admin.com / password — role super_admin
"""


# ── Slugify ────────────────────────────────────────────────────────────


class TestSlugify:
    def test_basic_email(self):
        assert _slugify("admin@admin.com") == "admin_admin_com"

    def test_dots_and_special(self):
        assert _slugify("first.last+tag@sub.example.com") == "first_last_tag_sub_example_com"

    def test_empty_falls_back(self):
        assert _slugify("") == "user"

    def test_collapses_underscores(self):
        # Multiple specials in a row should collapse to one underscore.
        assert "__" not in _slugify("a..b@c.com")


# ── Render: structure ──────────────────────────────────────────────────


class TestRender:
    def test_compiles_as_python(self):
        out = render_auth_contract_test_file(
            contract_markdown=_CONTRACT_FULL,
            primary_app_id="00000000-0000-0000-0000-000000000001",
        )
        # Will raise SyntaxError if malformed
        compile(out, "<rendered>", "exec")

    def test_has_jwks_test(self):
        out = render_auth_contract_test_file(
            contract_markdown=_CONTRACT_FULL,
            primary_app_id="app-1",
        )
        assert "def test_jwks_reachable_and_rs_signed" in out

    def test_has_per_user_tests(self):
        out = render_auth_contract_test_file(
            contract_markdown=_CONTRACT_FULL,
            primary_app_id="app-1",
        )
        assert "def test_user_login_admin_admin_com" in out
        assert "def test_user_login_landlord_example_com" in out
        assert "def test_user_login_tenant_example_com" in out

    def test_no_users_still_renders_jwks_test(self):
        out = render_auth_contract_test_file(
            contract_markdown=_CONTRACT_NO_USERS,
            primary_app_id="app-1",
        )
        compile(out, "<rendered>", "exec")
        assert "def test_jwks_reachable_and_rs_signed" in out
        # No user tests — nothing to grep for, but render shouldn't crash.

    def test_app_id_substituted(self):
        out = render_auth_contract_test_file(
            contract_markdown=_CONTRACT_FULL,
            primary_app_id="my-app-uuid",
        )
        assert '_APP_ID = "my-app-uuid"' in out

    def test_issuer_substituted_when_present(self):
        out = render_auth_contract_test_file(
            contract_markdown=_CONTRACT_FULL,
            primary_app_id="app-1",
        )
        assert '_ISSUER = "https://auth.example.com"' in out

    def test_issuer_assertion_skipped_when_absent(self):
        out = render_auth_contract_test_file(
            contract_markdown=_CONTRACT_NO_ISSUER,
            primary_app_id="app-1",
        )
        compile(out, "<rendered>", "exec")
        assert "no issuer in contract" in out


# ── Render: roles ──────────────────────────────────────────────────────


class TestRoleAssertions:
    def test_single_role_emitted(self):
        out = render_auth_contract_test_file(
            contract_markdown=_CONTRACT_FULL,
            primary_app_id="app-1",
        )
        # Find the tenant test, which has roles=['tenant']
        # The expected_roles list should contain 'tenant'.
        block = _extract_test_function(out, "test_user_login_tenant_example_com")
        assert "['tenant']" in block

    def test_multi_role_emitted(self):
        out = render_auth_contract_test_file(
            contract_markdown=_CONTRACT_FULL,
            primary_app_id="app-1",
        )
        block = _extract_test_function(out, "test_user_login_landlord_example_com")
        # Both landlord and manager should appear
        assert "landlord" in block
        assert "manager" in block


def _extract_test_function(source: str, fn_name: str) -> str:
    """Pull just the body of a top-level test function out of the
    rendered source so per-test assertions don't bleed."""
    lines = source.splitlines()
    out = []
    in_fn = False
    for line in lines:
        if line.startswith(f"def {fn_name}"):
            in_fn = True
            out.append(line)
            continue
        if in_fn:
            if line and not line.startswith((" ", "\t", ")")) and not line.startswith("def "):
                # Next top-level definition or docstring boundary
                pass
            if line.startswith("def ") and fn_name not in line:
                break
            out.append(line)
    return "\n".join(out)

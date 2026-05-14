"""Tests for FusionAuthOrchestrator.materialize(spec).

Mocks ``requests.request`` with a stateful fake — enough to verify the
materialize traversal makes the expected calls in the expected order
and respects soft-delete semantics for deprecated roles.
"""
from __future__ import annotations

import json as _json
from unittest.mock import patch, MagicMock

import pytest

from bizniz.auth_orchestrators import FusionAuthOrchestrator
from bizniz.auth_orchestrators.spec import (
    AppSpec,
    AuthSpec,
    AuthSpecDelta,
    DeprecatedRole,
    GroupSpec,
    RoleSpec,
    UserSpec,
)


class _FakeFA:
    """Stateful in-memory fake for FusionAuth REST.

    Records every call. Returns reasonable shapes for GET (404 on
    missing application/group/user, 200 with content otherwise) and
    treats POST/PUT as success.
    """
    def __init__(self):
        self.applications: dict = {}  # id → {name, roles: [{id, name, ...}]}
        self.users: dict = {}         # email → {id, registrations: [...]}
        self.groups: dict = {}        # id → {name, ...}
        self.calls: list = []

    def __call__(self, method, url, **kwargs):
        # Orchestrator sends full URLs; strip base for path matching.
        if url.startswith("http://fa"):
            url = url[len("http://fa"):]
        self.calls.append((method, url, kwargs.get("json")))
        # Application endpoints
        if url.startswith("/api/application/") and method == "GET":
            app_id = url.rsplit("/", 1)[-1]
            if app_id in self.applications:
                return self._resp(200, {"application": self.applications[app_id]})
            return self._resp(404, {})
        if url.startswith("/api/application/") and method == "POST":
            # ensure_application or ensure_role
            parts = url.split("/")
            app_id = parts[3]
            if len(parts) == 4:
                body = kwargs.get("json", {}).get("application") or {}
                self.applications[app_id] = {
                    "id": app_id,
                    "name": body.get("name", ""),
                    "roles": [],
                }
                return self._resp(200, {"application": self.applications[app_id]})
            if len(parts) == 5 and parts[4] == "role":
                body = kwargs.get("json", {}).get("role") or {}
                role_id = f"role-{body.get('name')}"
                self.applications.setdefault(app_id, {"roles": []})
                self.applications[app_id].setdefault("roles", []).append({
                    "id": role_id,
                    "name": body.get("name"),
                    "description": body.get("description", ""),
                    "isDefault": body.get("isDefault", False),
                    "isSuperRole": body.get("isSuperRole", False),
                })
                return self._resp(200, {"role": {"id": role_id}})

        # User lookup by email — orchestrator hits /api/user?email=...
        if url.startswith("/api/user?email=") and method == "GET":
            email = url.split("=", 1)[1]
            if email in self.users:
                return self._resp(200, {"user": self.users[email]})
            return self._resp(404, {})
        if url == "/api/user/registration" and method == "POST":
            body = kwargs.get("json", {})
            user = body.get("user", {})
            reg = body.get("registration", {})
            email = user.get("email")
            user_id = f"user-{email}"
            self.users[email] = {
                "id": user_id,
                "email": email,
                "registrations": [reg],
            }
            return self._resp(200, {
                "user": self.users[email],
                "registration": reg,
            })

        # Groups
        if url == "/api/group" and method == "GET":
            return self._resp(200, {"groups": list(self.groups.values())})
        if url == "/api/group" and method == "POST":
            body = kwargs.get("json", {}).get("group") or {}
            group_id = f"group-{body.get('name')}"
            self.groups[group_id] = {
                "id": group_id,
                "name": body.get("name"),
                "description": body.get("description", ""),
            }
            return self._resp(200, {"group": self.groups[group_id]})

        # Default: success no-op
        return self._resp(200, {})

    @staticmethod
    def _resp(status, body):
        m = MagicMock()
        m.status_code = status
        m.text = _json.dumps(body)
        m.json.return_value = body
        return m


@pytest.fixture
def fa():
    fake = _FakeFA()
    orch = FusionAuthOrchestrator(base_url="http://fa", api_key="k")
    with patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.request", side_effect=fake):
        yield orch, fake


def test_disabled_spec_is_noop(fa):
    orch, fake = fa
    report = orch.materialize(AuthSpec.baseline())
    assert report.actions == []
    assert fake.calls == []


def test_minimal_spec_creates_app_and_registers_seeded_admin(fa):
    orch, fake = fa
    spec = AuthSpec(
        enabled=True,
        applications=[AppSpec(name="Web")],
    )
    report = orch.materialize(spec)
    assert any(a.operation == "ensure_application" for a in report.actions)
    assert any(
        a.operation == "ensure_user" and "admin@admin.com" in a.target
        for a in report.actions
    )


def test_roles_registered_on_application(fa):
    orch, fake = fa
    spec = AuthSpec(
        enabled=True,
        roles=[
            RoleSpec(name="landlord"),
            RoleSpec(name="tenant"),
        ],
        applications=[AppSpec(name="Web")],
    )
    orch.materialize(spec)
    role_calls = [
        c for c in fake.calls
        if c[0] == "POST" and "/role" in c[1]
    ]
    role_names = {c[2]["role"]["name"] for c in role_calls}
    assert {"landlord", "tenant", "super_admin"} <= role_names


def test_test_user_only_registered_on_apps_with_matching_roles(fa):
    orch, fake = fa
    spec = AuthSpec(
        enabled=True,
        roles=[RoleSpec(name="admin"), RoleSpec(name="reader")],
        applications=[
            AppSpec(name="Admin", role_names=["admin"]),
            AppSpec(name="Reader", role_names=["reader"]),
        ],
        test_users=[
            UserSpec(email="alice@x", role_names=["admin"]),
        ],
    )
    report = orch.materialize(spec)
    # Filter to alice's ensure_user actions only — skip_user actions
    # are also emitted now (with explicit reason) for apps where she
    # has no role overlap.
    alice_ensures = [
        a for a in report.actions
        if a.operation == "ensure_user" and "alice@x" in a.target
    ]
    targets = {a.target for a in alice_ensures}
    assert any("@Admin" in t for t in targets)
    assert not any("@Reader" in t for t in targets)
    # ALSO assert the skip_user action exists for visibility.
    skips = [
        a for a in report.actions
        if a.operation == "skip_user" and "alice@x" in a.target
    ]
    assert any("@Reader" in s.target for s in skips), \
        "expected explicit skip_user action for alice@Reader"


def test_deprecated_roles_are_soft_deleted(fa):
    orch, fake = fa
    spec = AuthSpec(
        enabled=True,
        roles=[RoleSpec(name="active")],
        applications=[AppSpec(name="Web")],
        deprecated_roles=[
            DeprecatedRole(name="legacy_editor", deprecated_at="2026-05-04T00:00:00+00:00"),
        ],
    )
    report = orch.materialize(spec)
    soft = [a for a in report.actions if a.operation == "soft_delete_role"]
    assert len(soft) == 1
    assert "legacy_editor" in soft[0].target
    assert soft[0].applied is True
    # Critically: NO DELETE request was made.
    assert not any(c[0] == "DELETE" for c in fake.calls), \
        "soft-delete must not issue a DELETE request"


def test_groups_only_provisioned_when_enabled(fa):
    orch, fake = fa
    spec_off = AuthSpec(
        enabled=True,
        groups_enabled=False,
        roles=[RoleSpec(name="member")],
        applications=[AppSpec(name="Web")],
        groups=[GroupSpec(name="acme", role_names=["member"], application="Web")],
    )
    report_off = orch.materialize(spec_off)
    assert not any(a.operation == "ensure_group" for a in report_off.actions)

    fake.calls.clear()
    spec_on = spec_off.model_copy(update={"groups_enabled": True})
    report_on = orch.materialize(spec_on)
    assert any(a.operation == "ensure_group" for a in report_on.actions)


def test_publisher_aggregator_example_walks_through(fa):
    """End-to-end materialize on the design-doc publisher example."""
    orch, fake = fa
    spec = AuthSpec.baseline().apply(AuthSpecDelta(
        enable_auth=True,
        enable_groups=True,
        enable_multitenant=True,
        add_roles=[
            RoleSpec(name="publisher_admin"),
            RoleSpec(name="reader", is_default=True),
        ],
        add_applications=[
            AppSpec(name="Publisher Portal"),
            AppSpec(name="Reader Web", pkce_required=False),
        ],
        add_groups=[
            GroupSpec(
                name="acme-times",
                application="Publisher Portal",
                role_names=["publisher_admin"],
            ),
        ],
        add_test_users=[
            UserSpec(email="alice@acme-times.test", group_names=["acme-times"]),
            UserSpec(email="reader@example.test", role_names=["reader"]),
        ],
    ))
    report = orch.materialize(spec)
    ops = [a.operation for a in report.actions]
    assert "ensure_application" in ops
    assert "ensure_role" in ops
    assert "ensure_user" in ops
    assert "ensure_group" in ops
    # No errors anywhere
    assert all(a.error is None for a in report.actions), \
        f"unexpected errors: {[a for a in report.actions if a.error]}"

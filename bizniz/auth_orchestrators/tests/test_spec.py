"""Unit tests for AuthSpec / AuthSpecDelta."""
from __future__ import annotations

import pytest

from bizniz.auth_orchestrators.spec import (
    AppSpec,
    AuthSpec,
    AuthSpecDelta,
    GroupSpec,
    RoleSpec,
    SeedAdminSpec,
    UserSpec,
)


def test_baseline_is_disabled_and_empty():
    spec = AuthSpec.baseline()
    assert spec.enabled is False
    assert spec.roles == []
    assert spec.applications == []
    assert spec.groups == []
    assert spec.test_users == []
    assert spec.deprecated_roles == []


def test_baseline_seeded_admin_is_locked_default():
    spec = AuthSpec.baseline()
    assert spec.seeded_admin.email == "admin@admin.com"
    assert spec.seeded_admin.password == "password"
    assert spec.seeded_admin.password_change_required is True
    assert "super_admin" in spec.seeded_admin.role_names


def test_empty_delta_is_identity():
    spec = AuthSpec(
        enabled=True,
        roles=[RoleSpec(name="admin")],
        applications=[AppSpec(name="Web")],
    )
    out = spec.apply(AuthSpecDelta())
    assert out.enabled == spec.enabled
    assert [r.name for r in out.roles] == ["admin"]
    assert [a.name for a in out.applications] == ["Web"]


def test_delta_is_empty_check():
    assert AuthSpecDelta().is_empty() is True
    assert AuthSpecDelta(note="just a comment").is_empty() is True
    assert AuthSpecDelta(enable_auth=True).is_empty() is False
    assert AuthSpecDelta(add_roles=[RoleSpec(name="x")]).is_empty() is False


def test_delta_toggles_only_change_what_is_set():
    spec = AuthSpec(enabled=True, multitenant=True, groups_enabled=False)
    out = spec.apply(AuthSpecDelta(enable_groups=True))
    assert out.enabled is True
    assert out.multitenant is True
    assert out.groups_enabled is True


def test_delta_adds_roles():
    spec = AuthSpec.baseline()
    out = spec.apply(AuthSpecDelta(
        enable_auth=True,
        add_roles=[
            RoleSpec(name="landlord"),
            RoleSpec(name="tenant"),
        ],
    ))
    assert out.enabled is True
    assert sorted(r.name for r in out.roles) == ["landlord", "tenant"]


def test_delta_dedups_role_adds_by_name_later_wins():
    spec = AuthSpec(roles=[RoleSpec(name="admin", description="old")])
    out = spec.apply(AuthSpecDelta(
        add_roles=[RoleSpec(name="admin", description="new")],
    ))
    assert len(out.roles) == 1
    assert out.roles[0].description == "new"


def test_delta_remove_roles_is_soft_delete():
    spec = AuthSpec(roles=[
        RoleSpec(name="editor"),
        RoleSpec(name="viewer"),
    ])
    out = spec.apply(AuthSpecDelta(remove_roles=["editor"]))

    # Role removed from active list...
    assert [r.name for r in out.roles] == ["viewer"]
    # ...but recorded in deprecated_roles, not destroyed.
    assert len(out.deprecated_roles) == 1
    assert out.deprecated_roles[0].name == "editor"
    assert out.deprecated_roles[0].deprecated_at  # ISO timestamp


def test_delta_remove_roles_idempotent():
    """Removing a role twice doesn't double-record it as deprecated."""
    spec = AuthSpec(roles=[RoleSpec(name="editor")])
    after_first = spec.apply(AuthSpecDelta(remove_roles=["editor"]))
    after_second = after_first.apply(AuthSpecDelta(remove_roles=["editor"]))
    assert len(after_second.deprecated_roles) == 1


def test_delta_remove_roles_unknown_is_noop():
    spec = AuthSpec(roles=[RoleSpec(name="editor")])
    out = spec.apply(AuthSpecDelta(remove_roles=["never_existed"]))
    assert out.deprecated_roles == []
    assert [r.name for r in out.roles] == ["editor"]


def test_sequential_deltas_accumulate():
    """M1 → M2 → M3 deltas produce the cumulative spec."""
    m1 = AuthSpecDelta(
        enable_auth=True,
        add_roles=[RoleSpec(name="admin"), RoleSpec(name="user")],
        add_applications=[AppSpec(name="Web")],
        add_test_users=[UserSpec(email="alice@test", role_names=["admin"])],
    )
    m2 = AuthSpecDelta(
        enable_groups=True,
        add_roles=[RoleSpec(name="publisher_admin")],
        add_groups=[GroupSpec(name="acme", role_names=["publisher_admin"])],
    )
    m3 = AuthSpecDelta(remove_roles=["user"])

    spec = AuthSpec.baseline().apply(m1).apply(m2).apply(m3)

    assert spec.enabled is True
    assert spec.groups_enabled is True
    assert sorted(r.name for r in spec.roles) == ["admin", "publisher_admin"]
    assert [d.name for d in spec.deprecated_roles] == ["user"]
    assert [g.name for g in spec.groups] == ["acme"]
    assert [u.email for u in spec.test_users] == ["alice@test"]


def test_apply_is_pure():
    """apply() must not mutate the receiver."""
    spec = AuthSpec(roles=[RoleSpec(name="admin")])
    delta = AuthSpecDelta(add_roles=[RoleSpec(name="user")])
    out = spec.apply(delta)
    assert out is not spec
    assert [r.name for r in spec.roles] == ["admin"]  # unchanged
    assert [r.name for r in out.roles] == ["admin", "user"]


def test_role_name_must_be_nonempty():
    with pytest.raises(ValueError):
        RoleSpec(name="")
    with pytest.raises(ValueError):
        RoleSpec(name="   ")


def test_role_name_is_stripped():
    assert RoleSpec(name="  admin  ").name == "admin"


def test_user_dedup_by_email():
    spec = AuthSpec(test_users=[UserSpec(email="a@x", first_name="Old")])
    out = spec.apply(AuthSpecDelta(
        add_test_users=[UserSpec(email="a@x", first_name="New")],
    ))
    assert len(out.test_users) == 1
    assert out.test_users[0].first_name == "New"


def test_all_role_names_includes_seeded_admin():
    spec = AuthSpec(roles=[RoleSpec(name="user")])
    names = spec.all_role_names()
    assert "user" in names
    assert "super_admin" in names  # from seeded_admin


def test_seeded_admin_persists_across_deltas():
    spec = AuthSpec.baseline()
    out = spec.apply(AuthSpecDelta(enable_auth=True))
    assert out.seeded_admin.email == "admin@admin.com"
    assert out.seeded_admin.password_change_required is True

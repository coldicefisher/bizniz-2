"""Unit tests for render_kickstart()."""
from __future__ import annotations

from bizniz.auth.kickstart import _deterministic_uuid, render_kickstart
from bizniz.auth.spec import (
    AppSpec,
    AuthSpec,
    AuthSpecDelta,
    GroupSpec,
    RoleSpec,
    UserSpec,
)


def test_disabled_spec_renders_empty_kickstart():
    spec = AuthSpec.baseline()
    assert spec.enabled is False
    out = render_kickstart(spec)
    assert out == {"variables": {}, "apiKeys": [], "requests": []}


def test_minimal_enabled_spec_renders_seeded_admin_only():
    spec = AuthSpec(enabled=True)
    out = render_kickstart(spec)

    # variables, apiKeys, plus exactly one request: seeded admin
    assert out["variables"]["adminEmail"] == "admin@admin.com"
    assert out["variables"]["adminPassword"] == "password"
    assert len(out["apiKeys"]) == 1
    user_requests = [r for r in out["requests"] if "/api/user/" in r["url"]]
    assert len(user_requests) == 1
    assert user_requests[0]["body"]["user"]["email"] == "admin@admin.com"


def test_application_renders_with_roles():
    spec = AuthSpec(
        enabled=True,
        roles=[RoleSpec(name="landlord"), RoleSpec(name="tenant")],
        applications=[AppSpec(name="Property Web", redirect_urls=["http://x/cb"])],
    )
    out = render_kickstart(spec)
    app_requests = [r for r in out["requests"] if "/api/application/" in r["url"]]
    assert len(app_requests) == 1
    app_body = app_requests[0]["body"]["application"]
    assert app_body["name"] == "Property Web"
    role_names = [r["name"] for r in app_body["roles"]]
    assert "landlord" in role_names
    assert "tenant" in role_names
    assert "super_admin" in role_names  # implicit from seeded admin
    assert app_body["oauthConfiguration"]["authorizedRedirectURLs"] == ["http://x/cb"]


def test_test_user_only_registers_on_apps_with_matching_roles():
    spec = AuthSpec(
        enabled=True,
        roles=[RoleSpec(name="admin"), RoleSpec(name="reader")],
        applications=[
            AppSpec(name="Admin Portal", role_names=["admin"]),
            AppSpec(name="Reader Web", role_names=["reader"]),
        ],
        test_users=[
            UserSpec(email="alice@x", role_names=["admin"]),
            UserSpec(email="bob@x", role_names=["reader"]),
        ],
    )
    out = render_kickstart(spec)

    alice_req = next(
        r for r in out["requests"]
        if r.get("body", {}).get("user", {}).get("email") == "alice@x"
    )
    bob_req = next(
        r for r in out["requests"]
        if r.get("body", {}).get("user", {}).get("email") == "bob@x"
    )

    alice_app_ids = [reg["applicationId"] for reg in alice_req["body"]["user"]["registrations"]]
    bob_app_ids = [reg["applicationId"] for reg in bob_req["body"]["user"]["registrations"]]

    admin_id = _deterministic_uuid("application", "Admin Portal")
    reader_id = _deterministic_uuid("application", "Reader Web")

    assert alice_app_ids == [admin_id]  # not registered on Reader Web
    assert bob_app_ids == [reader_id]   # not registered on Admin Portal


def test_seeded_admin_registers_on_every_application():
    spec = AuthSpec(
        enabled=True,
        applications=[
            AppSpec(name="App One"),
            AppSpec(name="App Two"),
            AppSpec(name="App Three"),
        ],
    )
    out = render_kickstart(spec)
    admin_req = next(
        r for r in out["requests"]
        if r.get("body", {}).get("user", {}).get("email") == "admin@admin.com"
    )
    regs = admin_req["body"]["user"]["registrations"]
    assert len(regs) == 3


def test_groups_only_render_when_groups_enabled():
    spec = AuthSpec(
        enabled=True,
        groups_enabled=False,  # disabled
        roles=[RoleSpec(name="member")],
        applications=[AppSpec(name="App")],
        groups=[GroupSpec(name="acme", role_names=["member"], application="App")],
    )
    out = render_kickstart(spec)
    group_requests = [r for r in out["requests"] if "/api/group/" in r["url"]]
    assert group_requests == []  # gated off

    spec2 = spec.model_copy(update={"groups_enabled": True})
    out2 = render_kickstart(spec2)
    group_requests2 = [r for r in out2["requests"] if "/api/group/" in r["url"]]
    assert len(group_requests2) == 1


def test_deterministic_uuids_are_stable_across_calls():
    """Same name → same UUID. Critical for diffability."""
    a = _deterministic_uuid("role", "landlord")
    b = _deterministic_uuid("role", "landlord")
    assert a == b
    assert _deterministic_uuid("role", "landlord") != _deterministic_uuid("role", "tenant")


def test_full_kickstart_is_deterministic():
    """Same spec input → byte-identical kickstart output across runs."""
    spec = AuthSpec.baseline().apply(AuthSpecDelta(
        enable_auth=True,
        enable_groups=True,
        add_roles=[RoleSpec(name="publisher")],
        add_applications=[AppSpec(name="Publisher Portal")],
        add_groups=[GroupSpec(name="acme", role_names=["publisher"], application="Publisher Portal")],
        add_test_users=[UserSpec(email="alice@x", role_names=["publisher"])],
    ))
    a = render_kickstart(spec)
    b = render_kickstart(spec)
    assert a == b


def test_publisher_aggregator_example_renders():
    """End-to-end check that the design-doc example actually renders."""
    spec = AuthSpec.baseline().apply(AuthSpecDelta(
        enable_auth=True,
        enable_groups=True,
        enable_multitenant=True,
        add_roles=[
            RoleSpec(name="publisher_admin"),
            RoleSpec(name="publisher_editor"),
            RoleSpec(name="aggregator_admin"),
            RoleSpec(name="reader", is_default=True),
        ],
        add_applications=[
            AppSpec(name="Publisher Portal", redirect_urls=["https://pub.app/cb"]),
            AppSpec(name="Aggregator Admin", redirect_urls=["https://admin.app/cb"]),
            AppSpec(name="Reader Web", pkce_required=False),
        ],
        add_groups=[
            GroupSpec(
                name="acme-times",
                application="Publisher Portal",
                role_names=["publisher_admin", "publisher_editor"],
            ),
        ],
        add_test_users=[
            UserSpec(email="alice@acme-times.test", group_names=["acme-times"]),
            UserSpec(email="admin@aggregator.test", role_names=["aggregator_admin"]),
        ],
    ))
    out = render_kickstart(spec)

    # 3 apps + seeded admin + 2 test users + 1 group
    app_count = sum(1 for r in out["requests"] if "/api/application/" in r["url"])
    user_count = sum(1 for r in out["requests"] if "/api/user/" in r["url"])
    group_count = sum(1 for r in out["requests"] if "/api/group/" in r["url"])

    assert app_count == 3
    assert user_count == 3  # seeded admin + alice + admin@aggregator
    assert group_count == 1


def test_alice_via_group_gets_registered_on_publisher_portal():
    """User with no direct roles but a group membership should still
    end up registered on the group's application with the group's roles."""
    spec = AuthSpec.baseline().apply(AuthSpecDelta(
        enable_auth=True,
        enable_groups=True,
        add_roles=[RoleSpec(name="publisher_admin"), RoleSpec(name="publisher_editor")],
        add_applications=[AppSpec(name="Publisher Portal")],
        add_groups=[GroupSpec(
            name="acme",
            application="Publisher Portal",
            role_names=["publisher_admin", "publisher_editor"],
        )],
        add_test_users=[UserSpec(email="alice@x", group_names=["acme"])],
    ))
    out = render_kickstart(spec)
    alice_req = next(
        r for r in out["requests"]
        if r.get("body", {}).get("user", {}).get("email") == "alice@x"
    )
    regs = alice_req["body"]["user"]["registrations"]
    assert len(regs) == 1
    assert sorted(regs[0]["roles"]) == ["publisher_admin", "publisher_editor"]

"""AuthPlanner tests — single-call agent that emits AuthSpec."""
import json
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.auth_orchestrators.spec import AuthSpec
from bizniz.auth_planner.agent import AuthPlanner, AuthPlannerError
from bizniz.clients.base_ai_client import BaseAIClient


def _arch():
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=[
            ServiceDefinition(
                name="auth", service_type="auth", framework="fusionauth",
                language="yaml", description="", workspace_name="auth",
                port=9011,
            ),
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="", workspace_name="backend",
                port=8000,
            ),
        ],
    )


def _client_returning(payload: dict) -> BaseAIClient:
    c = MagicMock(spec=BaseAIClient)
    c.get_text.return_value = (json.dumps(payload), "j", [])
    return c


def _full_spec_payload():
    return {
        "enable_auth": True,
        "enable_groups": False,
        "enable_multitenant": False,
        "roles": [
            {"name": "super_admin", "description": "Platform admin",
             "is_super_role": True},
            {"name": "landlord", "description": "Property owner",
             "is_super_role": False},
            {"name": "tenant", "description": "Property occupant",
             "is_super_role": False},
        ],
        "applications": [
            {"name": "primary", "role_names": []},
        ],
        "test_users": [
            {"email": "landlord@example.com", "password": "password",
             "first_name": "Landlord", "last_name": "User",
             "role_names": ["landlord"]},
            {"email": "tenant@example.com", "password": "password",
             "first_name": "Tenant", "last_name": "User",
             "role_names": ["tenant"]},
        ],
    }


# ── Happy path ─────────────────────────────────────────────────────────


class TestHappyPath:
    def test_full_spec(self):
        client = _client_returning(_full_spec_payload())
        planner = AuthPlanner(client=client)
        spec = planner.plan(problem_slice="x", architecture=_arch())

        assert isinstance(spec, AuthSpec)
        assert spec.enabled is True
        assert {r.name for r in spec.roles} == {
            "super_admin", "landlord", "tenant",
        }
        assert {a.name for a in spec.applications} == {"primary"}
        assert {u.email for u in spec.test_users} == {
            "landlord@example.com", "tenant@example.com",
        }

    def test_spec_carries_user_metadata(self):
        client = _client_returning(_full_spec_payload())
        planner = AuthPlanner(client=client)
        spec = planner.plan(problem_slice="x", architecture=_arch())
        landlord = next(u for u in spec.test_users if u.email == "landlord@example.com")
        assert landlord.first_name == "Landlord"
        assert landlord.role_names == ["landlord"]
        assert landlord.password_change_required is False
        assert landlord.verified is True

    def test_super_admin_role_marked_super(self):
        client = _client_returning(_full_spec_payload())
        planner = AuthPlanner(client=client)
        spec = planner.plan(problem_slice="x", architecture=_arch())
        super_role = next(r for r in spec.roles if r.name == "super_admin")
        assert super_role.is_super_role is True


# ── Validation ─────────────────────────────────────────────────────────


class TestValidation:
    def test_zero_roles_raises(self):
        payload = _full_spec_payload()
        payload["roles"] = []
        client = _client_returning(payload)
        planner = AuthPlanner(client=client)
        with pytest.raises(AuthPlannerError, match="zero roles"):
            planner.plan(problem_slice="x", architecture=_arch())

    def test_zero_apps_raises(self):
        payload = _full_spec_payload()
        payload["applications"] = []
        client = _client_returning(payload)
        planner = AuthPlanner(client=client)
        with pytest.raises(AuthPlannerError, match="zero applications"):
            planner.plan(problem_slice="x", architecture=_arch())

    def test_user_referencing_unknown_role_raises(self):
        payload = _full_spec_payload()
        payload["test_users"].append({
            "email": "ghost@example.com", "password": "password",
            "first_name": "Ghost", "last_name": "User",
            "role_names": ["does_not_exist"],
        })
        client = _client_returning(payload)
        planner = AuthPlanner(client=client)
        with pytest.raises(AuthPlannerError, match="not in spec"):
            planner.plan(problem_slice="x", architecture=_arch())

    def test_user_referencing_super_admin_is_allowed(self):
        # The seeded admin has super_admin even though we may not list
        # the role explicitly; the validator must let users reference it.
        payload = _full_spec_payload()
        payload["test_users"].append({
            "email": "ops@example.com", "password": "password",
            "first_name": "Ops", "last_name": "User",
            "role_names": ["super_admin"],
        })
        client = _client_returning(payload)
        planner = AuthPlanner(client=client)
        spec = planner.plan(problem_slice="x", architecture=_arch())
        assert any(u.email == "ops@example.com" for u in spec.test_users)


# ── Prompt content ─────────────────────────────────────────────────────


class TestPromptContent:
    def test_prompt_includes_problem_slice(self):
        client = _client_returning(_full_spec_payload())
        planner = AuthPlanner(client=client)
        planner.plan(
            problem_slice="Landlords manage their properties.",
            architecture=_arch(),
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user_msg = next(m.content for m in sent if m.role == "user")
        assert "Landlords manage their properties" in user_msg

    def test_prompt_includes_services(self):
        client = _client_returning(_full_spec_payload())
        planner = AuthPlanner(client=client)
        planner.plan(problem_slice="x", architecture=_arch())
        sent = client.get_text.call_args.kwargs["messages"]
        user_msg = next(m.content for m in sent if m.role == "user")
        assert "backend" in user_msg
        assert "fastapi" in user_msg

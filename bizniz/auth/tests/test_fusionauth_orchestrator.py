"""Tests for FusionAuthOrchestrator (mock-based; no live FusionAuth).

The orchestrator is a thin wrapper over FusionAuth's REST API. We
mock ``requests.request`` to verify the operations dispatch to the
expected endpoints with the expected payloads, and that idempotent
methods don't double-create resources.
"""
from unittest.mock import patch, MagicMock

import pytest

from bizniz.auth import (
    FusionAuthError,
    FusionAuthOrchestrator,
    FusionAuthState,
    FusionAuthRole,
    FusionAuthUser,
)


def _resp(status_code: int, json_data: dict = None, text: str = ""):
    import json as _json
    m = MagicMock()
    m.status_code = status_code
    # Real responses always have text; the orchestrator short-
    # circuits to {} when text is empty, so synth from json.
    m.text = text or (_json.dumps(json_data) if json_data else "")
    m.json.return_value = json_data or {}
    return m


@pytest.fixture
def orch():
    return FusionAuthOrchestrator(
        base_url="http://localhost:9011",
        api_key="test-key",
    )


def test_request_returns_json_on_2xx(orch):
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.return_value = _resp(200, {"ok": True})
        result = orch.request("GET", "/api/status")
    assert result == {"ok": True}


def test_request_raises_on_4xx_unless_in_ok_statuses(orch):
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.return_value = _resp(404, {}, text="not found")
        with pytest.raises(FusionAuthError) as exc:
            orch.request("GET", "/api/role/missing")
    assert exc.value.status_code == 404


def test_request_accepts_widened_ok_statuses(orch):
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.return_value = _resp(404, {})
        # Caller explicitly tolerates 404 (e.g. delete-on-missing)
        result = orch.request("DELETE", "/api/user/x", ok_statuses=[200, 204, 404])
    assert result == {}


def test_request_raises_on_unreachable(orch):
    import requests as _r
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.side_effect = _r.ConnectionError("refused")
        with pytest.raises(FusionAuthError) as exc:
            orch.request("GET", "/api/status")
    assert "unreachable" in str(exc.value).lower()


def test_ensure_application_skips_when_exists(orch):
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.return_value = _resp(200, {"application": {"id": "app-1", "name": "X"}})
        result = orch.ensure_application("app-1", name="X")
    # Single request — only the GET, no POST to create.
    assert result == "app-1"
    assert m.call_count == 1


def test_ensure_application_creates_when_missing(orch):
    responses = [_resp(404, {}), _resp(200, {"application": {"id": "app-1"}})]
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.side_effect = responses
        result = orch.ensure_application("app-1", name="X")
    assert result == "app-1"
    # GET (404) + POST (create)
    assert m.call_count == 2


def test_ensure_role_idempotent(orch):
    """Existing role short-circuits the POST."""
    app_response = _resp(200, {
        "application": {
            "id": "app-1",
            "name": "X",
            "roles": [
                {"id": "role-1", "name": "landlord", "description": "Manages props"}
            ],
        }
    })
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.return_value = app_response
        result = orch.ensure_role("app-1", name="landlord")
    assert result == "role-1"
    # Only the get_application call — no POST to create.
    assert m.call_count == 1


def test_ensure_role_creates_when_missing(orch):
    responses = [
        _resp(200, {
            "application": {"id": "app-1", "name": "X", "roles": []},
        }),
        _resp(200, {"role": {"id": "role-2"}}),
    ]
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.side_effect = responses
        role_id = orch.ensure_role("app-1", name="tenant", description="renter")
    assert role_id == "role-2"
    # POST should include role body
    create_call = m.call_args_list[1]
    assert create_call.kwargs["method"] == "POST"
    assert "/api/application/app-1/role" in create_call.kwargs["url"]


def test_get_token_extracts_token_from_response(orch):
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.return_value = _resp(200, {"token": "jwt-abc"})
        token = orch.get_token("app-1", "u@example.com", "pw")
    assert token == "jwt-abc"


def test_get_token_raises_when_token_missing(orch):
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.return_value = _resp(200, {"twoFactorId": "abc"})
        with pytest.raises(FusionAuthError) as exc:
            orch.get_token("app-1", "u@example.com", "pw")
    assert "no token" in str(exc.value).lower()


def test_delete_user_tolerates_404(orch):
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.return_value = _resp(404, {})
        # Should not raise
        orch.delete_user("user-id")
    assert m.call_count == 1


def test_assign_role_idempotent_when_already_present(orch):
    user_get = _resp(200, {
        "user": {
            "id": "user-1",
            "registrations": [
                {"applicationId": "app-1", "roles": ["landlord"]},
            ],
        }
    })
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.return_value = user_get
        orch.assign_role("app-1", "user-1", "landlord")
    # Single GET — no PUT since role already present.
    assert m.call_count == 1


def test_assign_role_adds_new_role(orch):
    responses = [
        _resp(200, {
            "user": {
                "id": "user-1",
                "registrations": [
                    {"applicationId": "app-1", "roles": ["landlord"]},
                ],
            }
        }),
        _resp(200, {}),
    ]
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.side_effect = responses
        orch.assign_role("app-1", "user-1", "admin")
    # GET + PUT
    assert m.call_count == 2
    put_call = m.call_args_list[1]
    assert put_call.kwargs["method"] == "PUT"
    body = put_call.kwargs["json"]
    assert "admin" in body["registration"]["roles"]
    assert "landlord" in body["registration"]["roles"]


def test_unassign_role_idempotent_when_absent(orch):
    user_get = _resp(200, {
        "user": {
            "id": "user-1",
            "registrations": [
                {"applicationId": "app-1", "roles": ["landlord"]},
            ],
        }
    })
    with patch("bizniz.auth.fusionauth_orchestrator.requests.request") as m:
        m.return_value = user_get
        orch.unassign_role("app-1", "user-1", "admin")
    # Only the GET — no PUT since role wasn't there.
    assert m.call_count == 1


def test_typed_state_round_trips():
    """Typed entities serialize meaningfully — used by reconcile()."""
    state = FusionAuthState(
        application_id="app-1",
        application_name="Property Manager",
        roles=[
            FusionAuthRole(role_id="r1", name="landlord"),
            FusionAuthRole(role_id="r2", name="tenant"),
        ],
        users=[
            FusionAuthUser(
                user_id="u1", email="landlord@example.com",
                roles=["landlord"],
            ),
        ],
    )
    assert state.application_id == "app-1"
    assert len(state.roles) == 2
    assert state.users[0].roles == ["landlord"]

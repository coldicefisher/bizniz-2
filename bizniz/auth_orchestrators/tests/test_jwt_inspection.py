"""Tests for the orchestrator's JWT/JWKS/tenant inspection methods."""
from __future__ import annotations

import json as _json
from unittest.mock import patch, MagicMock

import pytest

from bizniz.auth_orchestrators import FusionAuthOrchestrator


def _resp(status, json_data=None, text=""):
    m = MagicMock()
    m.status_code = status
    m.text = text or (_json.dumps(json_data) if json_data else "")
    m.json.return_value = json_data or {}
    return m


@pytest.fixture
def orch():
    return FusionAuthOrchestrator(base_url="http://fa", api_key="k")


def test_get_jwks_returns_body(orch):
    with patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.get") as m:
        m.return_value = _resp(200, {"keys": [{"kid": "abc", "alg": "RS256"}]})
        result = orch.get_jwks()
    assert result["keys"][0]["kid"] == "abc"


def test_get_jwks_raises_on_unreachable(orch):
    import requests as _requests
    with patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.get") as m:
        m.side_effect = _requests.RequestException("connection refused")
        with pytest.raises(Exception):
            orch.get_jwks()


def test_generate_signing_key_skips_when_already_exists(orch):
    """Idempotent: if a key already exists at this ID, don't recreate."""
    with patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.request") as m:
        m.return_value = _resp(200, {"key": {"id": "k1", "algorithm": "RS256"}})
        kid = orch.generate_signing_key(key_id="k1")
    assert kid == "k1"
    # GET (existence check), no POST
    methods = [c.kwargs.get("method") or c.args[0] for c in m.call_args_list]
    assert "GET" in methods
    assert "POST" not in methods


def test_generate_signing_key_creates_when_missing(orch):
    with patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.request") as m:
        # First call: GET key/k1 → 404. Second: POST generate → 200.
        m.side_effect = [_resp(404, {}), _resp(200, {"key": {"id": "k1"}})]
        kid = orch.generate_signing_key(
            key_id="k1", algorithm="RS256", length=2048,
        )
    assert kid == "k1"
    # POST body has the algo + length
    post_call = m.call_args_list[-1]
    body = post_call.kwargs.get("json")
    assert body["key"]["algorithm"] == "RS256"
    assert body["key"]["length"] == 2048


def test_set_tenant_signing_key_patches_jwt_config_without_name(orch):
    """First-attempt PATCH omits name (works on mature tenants and
    avoids FA's duplicate-name trap on fresh tenants)."""
    with patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.request") as m:
        m.return_value = _resp(200, {})
        orch.set_tenant_signing_key(tenant_id="t1", key_id="k1")

    patch_call = m.call_args_list[-1]
    body = patch_call.kwargs.get("json")
    assert "name" not in body["tenant"]
    assert body["tenant"]["jwtConfiguration"]["accessTokenKeyId"] == "k1"
    assert body["tenant"]["jwtConfiguration"]["idTokenKeyId"] == "k1"


def test_patch_tenant_retries_with_name_on_blank_name_error(orch):
    """When FA rejects a name-less PATCH with a tenant.name error,
    we re-fetch the tenant's actual name and retry with it included.
    Covers the fresh-tenant case where FA's validator demands name."""
    with patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.request") as m:
        m.side_effect = [
            # First PATCH (no name) → 400 with tenant.name error
            _resp(400, {}, text='{"fieldErrors":{"tenant.name":[{"code":"[blank]tenant.name"}]}}'),
            # GET tenant for the retry
            _resp(200, {"tenant": {"id": "t1", "name": "Default"}}),
            # Second PATCH (with name) → 200
            _resp(200, {}),
        ]
        orch.patch_tenant("t1", {"jwtConfiguration": {"accessTokenKeyId": "k1"}})

    # Three calls total: PATCH, GET, PATCH
    assert m.call_count == 3
    last_patch = m.call_args_list[-1]
    body = last_patch.kwargs.get("json")
    assert body["tenant"]["name"] == "Default"
    assert body["tenant"]["jwtConfiguration"]["accessTokenKeyId"] == "k1"


def test_patch_tenant_does_not_retry_on_unrelated_400(orch):
    """A 400 that isn't a tenant.name issue should propagate, not
    trigger a retry."""
    from bizniz.auth_orchestrators.types import FusionAuthError
    with patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.request") as m:
        m.return_value = _resp(400, {}, text="some other validation error")
        with pytest.raises(FusionAuthError):
            orch.patch_tenant("t1", {"foo": "bar"})
    assert m.call_count == 1


def test_diagnose_jwt_setup_flags_empty_jwks(orch):
    """The motivating bug: HS256 default → JWKS exposes 0 keys."""
    with patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.get") as gm, \
         patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.request") as rm:
        gm.return_value = _resp(200, {"keys": []})
        # GET tenant: returns a tenant with HS256 access token key
        rm.return_value = _resp(200, {
            "tenant": {
                "id": "t1",
                "name": "Default",
                "jwtConfiguration": {"accessTokenKeyId": "default-hmac"},
            },
        })

        report = orch.diagnose_jwt_setup(
            tenant_id="t1", app_id="app-1",
        )

    assert report["ok"] is False
    assert report["jwks_keys"] == 0
    assert any("0 keys" in e for e in report["errors"])


def test_diagnose_jwt_setup_passes_when_keys_present(orch):
    with patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.get") as gm, \
         patch("bizniz.auth_orchestrators.fusionauth_orchestrator.requests.request") as rm:
        gm.return_value = _resp(200, {"keys": [
            {"kid": "abc", "alg": "RS256", "kty": "RSA"},
        ]})
        # Multiple GETs: tenant (with key id), then signing key
        rm.side_effect = [
            _resp(200, {
                "tenant": {
                    "id": "t1", "name": "Default",
                    "jwtConfiguration": {"accessTokenKeyId": "abc"},
                },
            }),
            _resp(200, {"key": {"id": "abc", "algorithm": "RS256"}}),
        ]

        report = orch.diagnose_jwt_setup(
            tenant_id="t1", app_id="app-1",
        )

    assert report["ok"] is True
    assert report["jwks_keys"] == 1
    assert report["signing_key_algorithm"] == "RS256"
    assert report["errors"] == []

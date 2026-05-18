"""Unit tests for the FusionAuth client exception hierarchy.

Covers the three exception classes plus the ``_redact`` helper that
enforces password-never-logged. These exceptions are raised by every
function in ``app.services.fusionauth_client`` and route handlers
``isinstance``-dispatch on the subclasses to map to HTTP status, so
the hierarchy contract is load-bearing.
"""
import pytest

from app.services.fusionauth_client import (
    FusionAuthError,
    FusionAuthUnavailable,
    FusionAuthValidationError,
    _redact,
    _REDACT_KEYS,
)


@pytest.mark.unit
class TestRedactKeys:
    """``_REDACT_KEYS`` is the source of truth for what gets masked."""

    def test_redact_keys_includes_password_fields(self):
        assert "password" in _REDACT_KEYS
        assert "currentPassword" in _REDACT_KEYS
        assert "newPassword" in _REDACT_KEYS

    def test_redact_keys_is_a_set(self):
        assert isinstance(_REDACT_KEYS, set)


@pytest.mark.unit
class TestRedactHelper:
    """``_redact`` recursively masks password-like values."""

    def test_redact_passthrough_for_none(self):
        assert _redact(None) is None

    def test_redact_passthrough_for_string(self):
        assert _redact("hello") == "hello"

    def test_redact_passthrough_for_int(self):
        assert _redact(42) == 42

    def test_redact_top_level_password(self):
        result = _redact({"email": "u@e.com", "password": "Secret123!"})
        assert result == {"email": "u@e.com", "password": "***"}

    def test_redact_top_level_current_password(self):
        result = _redact({"currentPassword": "old"})
        assert result == {"currentPassword": "***"}

    def test_redact_top_level_new_password(self):
        result = _redact({"newPassword": "shiny"})
        assert result == {"newPassword": "***"}

    def test_redact_nested_dict(self):
        body = {"user": {"email": "u@e.com", "password": "p"}}
        assert _redact(body) == {"user": {"email": "u@e.com", "password": "***"}}

    def test_redact_inside_list(self):
        body = [{"password": "x"}, {"password": "y"}]
        assert _redact(body) == [{"password": "***"}, {"password": "***"}]

    def test_redact_mixed_nested_structure(self):
        body = {
            "registrations": [
                {"user": {"email": "a@b.com", "password": "Pa55!"}},
                {"user": {"email": "c@d.com", "password": "Other!"}},
            ],
            "newPassword": "rotate",
            "safe": "keep-me",
        }
        result = _redact(body)
        assert result == {
            "registrations": [
                {"user": {"email": "a@b.com", "password": "***"}},
                {"user": {"email": "c@d.com", "password": "***"}},
            ],
            "newPassword": "***",
            "safe": "keep-me",
        }

    def test_redact_does_not_mutate_input(self):
        body = {"password": "leak-me-not"}
        _redact(body)
        assert body == {"password": "leak-me-not"}

    def test_redact_preserves_unrelated_keys(self):
        body = {"email": "u@e.com", "displayName": "Cook"}
        assert _redact(body) == body


@pytest.mark.unit
class TestFusionAuthErrorInit:
    """Base class stores all three attributes verbatim."""

    def test_stores_status_code_body_message(self):
        err = FusionAuthError(400, {"x": 1}, "bad request")
        assert err.status_code == 400
        assert err.body == {"x": 1}
        assert err.message == "bad request"

    def test_message_defaults_to_empty_string(self):
        err = FusionAuthError(503, None)
        assert err.message == ""

    def test_accepts_none_status_code(self):
        err = FusionAuthError(None, None, "connection refused")
        assert err.status_code is None
        assert err.body is None

    def test_accepts_string_body(self):
        err = FusionAuthError(502, "Bad Gateway")
        assert err.body == "Bad Gateway"

    def test_accepts_dict_body(self):
        body = {"fieldErrors": {"user.password": [{"code": "weak"}]}}
        err = FusionAuthError(400, body)
        assert err.body == body

    def test_is_an_exception(self):
        err = FusionAuthError(500, None)
        assert isinstance(err, Exception)
        with pytest.raises(FusionAuthError):
            raise FusionAuthError(500, None)


@pytest.mark.unit
class TestFusionAuthErrorStr:
    """``__str__`` must include status_code and a redacted body."""

    def test_str_includes_status_code(self):
        s = str(FusionAuthError(404, {"foo": "bar"}))
        assert "404" in s

    def test_str_redacts_password(self):
        body = {"email": "u@e.com", "password": "Plaintext123!"}
        s = str(FusionAuthError(400, body))
        assert "Plaintext123!" not in s
        assert "***" in s

    def test_str_redacts_nested_password(self):
        body = {"user": {"password": "do-not-leak"}}
        s = str(FusionAuthError(400, body))
        assert "do-not-leak" not in s
        assert "***" in s

    def test_str_redacts_current_and_new_password(self):
        body = {"currentPassword": "old-secret", "newPassword": "new-secret"}
        s = str(FusionAuthError(400, body))
        assert "old-secret" not in s
        assert "new-secret" not in s

    def test_str_includes_message_when_provided(self):
        s = str(FusionAuthError(500, None, "boom"))
        assert "boom" in s

    def test_str_handles_none_body(self):
        s = str(FusionAuthError(None, None, "timeout"))
        assert "None" in s or "status_code=None" in s
        assert "timeout" in s


@pytest.mark.unit
class TestExceptionSubclasses:
    """Subclasses inherit base behavior and remain distinguishable."""

    def test_unavailable_is_a_fusionauth_error(self):
        err = FusionAuthUnavailable(503, {"error": "boom"})
        assert isinstance(err, FusionAuthError)
        assert err.status_code == 503

    def test_validation_error_is_a_fusionauth_error(self):
        err = FusionAuthValidationError(400, {"fieldErrors": {}})
        assert isinstance(err, FusionAuthError)
        assert err.status_code == 400

    def test_subclasses_are_distinct(self):
        unavailable = FusionAuthUnavailable(503, None)
        validation = FusionAuthValidationError(400, None)
        assert not isinstance(unavailable, FusionAuthValidationError)
        assert not isinstance(validation, FusionAuthUnavailable)

    def test_unavailable_str_redacts_password(self):
        s = str(FusionAuthUnavailable(503, {"password": "leak"}))
        assert "leak" not in s
        assert "***" in s

    def test_validation_error_str_redacts_password(self):
        s = str(FusionAuthValidationError(400, {"password": "leak"}))
        assert "leak" not in s
        assert "***" in s

    def test_subclass_name_in_str(self):
        s = str(FusionAuthUnavailable(503, None))
        assert "FusionAuthUnavailable" in s
        s2 = str(FusionAuthValidationError(400, None))
        assert "FusionAuthValidationError" in s2

    def test_can_be_raised_and_caught_as_base(self):
        with pytest.raises(FusionAuthError):
            raise FusionAuthUnavailable(503, None)
        with pytest.raises(FusionAuthError):
            raise FusionAuthValidationError(400, None)

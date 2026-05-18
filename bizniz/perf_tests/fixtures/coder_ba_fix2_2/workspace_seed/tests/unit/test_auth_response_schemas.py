"""Unit tests for the BE-003-U1 auth response schemas.

Covers UserOut (incl. from_attributes mapping from the User ORM
model), AuthResponse (nested UserOut), and ErrorResponse (with and
without the optional ``fields`` dict). Critically asserts that none
of the response schemas accept or emit a ``password`` field.
"""
import uuid

import pytest
from pydantic import ValidationError

from app.models.user import User
from app.schemas.auth import AuthResponse, ErrorResponse, UserOut


@pytest.mark.unit
class TestUserOutSchema:
    def test_construct_with_all_fields(self):
        uid = uuid.uuid4()
        u = UserOut(
            id=uid,
            email="cook@example.com",
            display_name="Chef",
            role="user",
        )
        assert u.id == uid
        assert u.email == "cook@example.com"
        assert u.display_name == "Chef"
        assert u.role == "user"

    def test_display_name_optional_defaults_none(self):
        u = UserOut(id=uuid.uuid4(), email="cook@example.com", role="user")
        assert u.display_name is None

    def test_id_must_be_uuid(self):
        with pytest.raises(ValidationError):
            UserOut(id="not-a-uuid", email="cook@example.com", role="user")

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            UserOut(id=uuid.uuid4(), email="cook@example.com")  # no role

    def test_from_attributes_builds_from_user_orm(self):
        """ConfigDict(from_attributes=True) lets validation read User attrs."""
        uid = uuid.uuid4()
        orm = User(
            id=uid,
            email="cook@example.com",
            role="admin",
            display_name="Admin Cook",
        )
        u = UserOut.model_validate(orm)
        assert u.id == uid
        assert u.email == "cook@example.com"
        assert u.role == "admin"
        assert u.display_name == "Admin Cook"

    def test_no_password_field_on_class(self):
        """Response models MUST NOT expose any password field."""
        assert "password" not in UserOut.model_fields
        assert "password_hash" not in UserOut.model_fields

    def test_extra_password_field_is_ignored_on_input(self):
        """Even if a caller injects 'password', it must not surface on output."""
        u = UserOut(
            id=uuid.uuid4(),
            email="cook@example.com",
            role="user",
            password="should-not-appear",  # type: ignore[call-arg]
        )
        dumped = u.model_dump()
        assert "password" not in dumped


@pytest.mark.unit
class TestAuthResponseSchema:
    def test_construct_with_token_and_user(self):
        user = UserOut(
            id=uuid.uuid4(),
            email="cook@example.com",
            display_name=None,
            role="user",
        )
        resp = AuthResponse(token="jwt.abc.def", user=user)
        assert resp.token == "jwt.abc.def"
        assert resp.user.email == "cook@example.com"

    def test_token_required(self):
        user = UserOut(id=uuid.uuid4(), email="x@y.z", role="user")
        with pytest.raises(ValidationError):
            AuthResponse(user=user)  # type: ignore[call-arg]

    def test_user_required(self):
        with pytest.raises(ValidationError):
            AuthResponse(token="jwt.abc.def")  # type: ignore[call-arg]

    def test_nested_user_validated(self):
        with pytest.raises(ValidationError):
            AuthResponse(
                token="jwt.abc.def",
                user={"id": "not-a-uuid", "email": "x@y.z", "role": "user"},
            )

    def test_no_password_field_on_class(self):
        assert "password" not in AuthResponse.model_fields


@pytest.mark.unit
class TestErrorResponseSchema:
    def test_error_only(self):
        e = ErrorResponse(error="invalid_credentials")
        assert e.error == "invalid_credentials"
        assert e.fields is None

    def test_error_with_field_details(self):
        e = ErrorResponse(
            error="validation_error",
            fields={"email": "required", "password": "required"},
        )
        assert e.fields == {"email": "required", "password": "required"}

    def test_error_required(self):
        with pytest.raises(ValidationError):
            ErrorResponse()  # type: ignore[call-arg]

    def test_dump_omits_password_keys(self):
        """The schema itself has no password field; sanity-check the dump."""
        e = ErrorResponse(error="bad")
        dumped = e.model_dump()
        assert "password" not in dumped

"""Unit tests for BE-003 auth schemas.

Covers SignupRequest, LoginRequest, UserOut, AuthResponse, and
ErrorResponse: email normalization, regex/length validation,
display_name trim rules, password min_length, ORM-attribute build,
round-trip serialization, and the security invariant that no
password field ever leaks into the response models.
"""
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.schemas.auth import (
    AuthResponse,
    ErrorResponse,
    LoginRequest,
    SignupRequest,
    UserOut,
)


@pytest.mark.unit
class TestSignupRequest:
    def test_signup_happy_path(self):
        """Email lowercased and display_name trimmed on the happy path."""
        result = SignupRequest(
            email="Alice@Example.com",
            password="hunter2",
            display_name="  Alice  ",
        )
        assert result.email == "alice@example.com"
        assert result.display_name == "Alice"
        assert result.password == "hunter2"

    def test_signup_email_lowercased(self):
        """Mixed-case email in, lowercase email out."""
        result = SignupRequest(
            email="MixedCase@DOMAIN.COM", password="hunter2"
        )
        assert result.email == "mixedcase@domain.com"

    @pytest.mark.parametrize("bad_email", ["no-at-sign", "two@@signs.com"])
    def test_signup_email_regex_rejects(self, bad_email):
        """Emails without exactly one @ or missing dot are rejected."""
        with pytest.raises(ValidationError):
            SignupRequest(email=bad_email, password="hunter2")

    def test_signup_email_max_length_254(self):
        """A 255-char email is rejected; a 254-char valid email passes."""
        # Build a 255-char email: must be > 254 to fail.
        local_too_long = "a" * 250
        too_long = f"{local_too_long}@b.co"  # 250 + 1 + 2 + 1 + 2 = 256? compute
        # length = 250 + len('@b.co') = 250 + 5 = 255
        assert len(too_long) == 255
        with pytest.raises(ValidationError):
            SignupRequest(email=too_long, password="hunter2")

        # Now build a valid 254-char email.
        local_ok = "a" * 249
        ok_email = f"{local_ok}@b.co"  # 249 + 5 = 254
        assert len(ok_email) == 254
        result = SignupRequest(email=ok_email, password="hunter2")
        assert result.email == ok_email.lower()

    def test_signup_password_min_length(self):
        """Empty password is rejected at the schema layer."""
        with pytest.raises(ValidationError):
            SignupRequest(email="a@b.co", password="")

    def test_signup_display_name_empty_after_trim(self):
        """Whitespace-only display_name fails with a clear message."""
        with pytest.raises(ValidationError) as exc_info:
            SignupRequest(
                email="a@b.co", password="hunter2", display_name="   "
            )
        # Message should mention 'empty after trim' or similar.
        msg = str(exc_info.value).lower()
        assert "empty" in msg or "trim" in msg

    def test_signup_display_name_max_100(self):
        """101-char display_name is rejected by Field(max_length=100)."""
        with pytest.raises(ValidationError):
            SignupRequest(
                email="a@b.co",
                password="hunter2",
                display_name="x" * 101,
            )

    def test_signup_display_name_none_allowed(self):
        """Omitting display_name leaves it as None."""
        result = SignupRequest(email="a@b.co", password="hunter2")
        assert result.display_name is None


@pytest.mark.unit
class TestLoginRequest:
    def test_login_happy_path(self):
        """LoginRequest lowercases email on the happy path."""
        result = LoginRequest(email="Bob@Example.com", password="x")
        assert result.email == "bob@example.com"
        assert result.password == "x"

    def test_login_email_regex_rejects(self):
        """Invalid email syntax raises ValidationError."""
        with pytest.raises(ValidationError):
            LoginRequest(email="not-an-email", password="x")


@pytest.mark.unit
class TestUserOut:
    def test_user_out_from_attributes(self):
        """UserOut can be built from any object with matching attrs."""
        uid = uuid4()
        ns = SimpleNamespace(
            id=uid,
            email="a@b.c",
            display_name=None,
            role="user",
        )
        result = UserOut.model_validate(ns, from_attributes=True)
        assert result.id == uid
        assert isinstance(result.id, UUID)
        assert result.email == "a@b.c"
        assert result.display_name is None
        assert result.role == "user"


@pytest.mark.unit
class TestAuthResponse:
    def test_auth_response_round_trip(self):
        """AuthResponse serializes token + user with no password key anywhere."""
        user = UserOut(
            id=uuid4(),
            email="a@b.c",
            display_name="Alice",
            role="user",
        )
        resp = AuthResponse(token="abc", user=user)
        dumped = resp.model_dump()

        assert dumped["token"] == "abc"
        assert "user" in dumped
        assert dumped["user"]["email"] == "a@b.c"
        assert dumped["user"]["role"] == "user"

        # Security invariant: no password fields anywhere in the dump.
        def _assert_no_password(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    assert "password" not in k.lower(), (
                        f"AuthResponse leaked a password-like key: {k}"
                    )
                    _assert_no_password(v)
            elif isinstance(obj, list):
                for item in obj:
                    _assert_no_password(item)

        _assert_no_password(dumped)


@pytest.mark.unit
class TestErrorResponse:
    def test_error_response_optional_fields(self):
        """ErrorResponse.fields defaults to None and accepts a dict."""
        bare = ErrorResponse(error="bad")
        assert bare.error == "bad"
        assert bare.fields is None

        with_fields = ErrorResponse(error="bad", fields={"email": "taken"})
        assert with_fields.error == "bad"
        assert with_fields.fields == {"email": "taken"}

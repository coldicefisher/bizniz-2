"""Unit tests for the BE-003-U3 LoginRequest schema.

Covers email regex validation, lowercasing, max_length, and
password min_length. LoginRequest does NOT carry display_name.
"""
import pytest
from pydantic import ValidationError

from app.schemas.auth import LoginRequest


@pytest.mark.unit
class TestLoginRequestHappyPath:
    def test_minimal_valid_payload(self):
        req = LoginRequest(email="user@example.com", password="password")
        assert req.email == "user@example.com"
        assert req.password == "password"

    def test_does_not_have_display_name_field(self):
        # display_name lives on SignupRequest, not LoginRequest.
        assert "display_name" not in LoginRequest.model_fields


@pytest.mark.unit
class TestLoginRequestEmailNormalization:
    def test_email_lowercased(self):
        req = LoginRequest(email="User@Example.COM", password="p")
        assert req.email == "user@example.com"

    def test_email_already_lowercase_preserved(self):
        req = LoginRequest(email="a@b.co", password="p")
        assert req.email == "a@b.co"


@pytest.mark.unit
class TestLoginRequestEmailValidation:
    def test_malformed_email_no_at_rejected(self):
        with pytest.raises(ValidationError):
            LoginRequest(email="not-an-email", password="p")

    def test_email_without_dot_rejected(self):
        with pytest.raises(ValidationError):
            LoginRequest(email="foo@bar", password="p")

    def test_email_with_whitespace_rejected(self):
        with pytest.raises(ValidationError):
            LoginRequest(email="foo @bar.com", password="p")

    def test_email_too_long_rejected(self):
        local = "a" * 250
        too_long = f"{local}@b.co"  # 255 chars
        assert len(too_long) > 254
        with pytest.raises(ValidationError):
            LoginRequest(email=too_long, password="p")


@pytest.mark.unit
class TestLoginRequestPassword:
    def test_password_required(self):
        with pytest.raises(ValidationError):
            LoginRequest(email="a@b.co")  # type: ignore[call-arg]

    def test_password_min_length_one(self):
        req = LoginRequest(email="a@b.co", password="x")
        assert req.password == "x"

    def test_password_empty_rejected(self):
        with pytest.raises(ValidationError):
            LoginRequest(email="a@b.co", password="")

    def test_password_complexity_NOT_enforced_here(self):
        # Complexity is FusionAuth's job; LoginRequest accepts any non-empty.
        req = LoginRequest(email="a@b.co", password="weak")
        assert req.password == "weak"

    def test_password_whitespace_passed_through(self):
        # Whitespace inside passwords is legal; do not trim.
        req = LoginRequest(email="a@b.co", password="  pw  ")
        assert req.password == "  pw  "


@pytest.mark.unit
class TestLoginRequestRequiredFields:
    def test_email_required(self):
        with pytest.raises(ValidationError):
            LoginRequest(password="p")  # type: ignore[call-arg]

    def test_empty_body_rejected(self):
        with pytest.raises(ValidationError):
            LoginRequest()  # type: ignore[call-arg]


@pytest.mark.unit
class TestLoginRequestSharedRegex:
    def test_login_and_signup_share_email_regex_constant(self):
        # DRY check: both schemas should reference the same module-level constant.
        from app.schemas import auth as auth_schemas

        assert hasattr(auth_schemas, "_EMAIL_RE")
        # Pattern matches the spec exactly.
        assert auth_schemas._EMAIL_RE.pattern == r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

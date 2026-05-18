"""Unit tests for the BE-003-U2 SignupRequest schema.

Covers email regex validation, lowercasing, display_name trim,
empty-after-trim rejection, max_length constraints, password
min_length, and the no-password-complexity rule.
"""
import pytest
from pydantic import ValidationError

from app.schemas.auth import SignupRequest


@pytest.mark.unit
class TestSignupRequestHappyPath:
    def test_minimal_valid_payload(self):
        req = SignupRequest(email="cook@example.com", password="Password123!")
        assert req.email == "cook@example.com"
        assert req.password == "Password123!"
        assert req.display_name is None

    def test_full_valid_payload(self):
        req = SignupRequest(
            email="cook@example.com",
            password="Password123!",
            display_name="Chef",
        )
        assert req.display_name == "Chef"


@pytest.mark.unit
class TestSignupRequestEmailNormalization:
    def test_email_lowercased(self):
        req = SignupRequest(email="User@Example.COM", password="p")
        assert req.email == "user@example.com"

    def test_email_already_lowercase_preserved(self):
        req = SignupRequest(email="a@b.co", password="p")
        assert req.email == "a@b.co"


@pytest.mark.unit
class TestSignupRequestEmailValidation:
    def test_malformed_email_no_at_rejected(self):
        with pytest.raises(ValidationError):
            SignupRequest(email="not-an-email", password="p")

    def test_email_without_dot_rejected(self):
        # EmailStr itself rejects "foo@bar" but the regex would too.
        with pytest.raises(ValidationError):
            SignupRequest(email="foo@bar", password="p")

    def test_email_with_whitespace_rejected(self):
        with pytest.raises(ValidationError):
            SignupRequest(email="foo @bar.com", password="p")

    def test_email_too_long_rejected(self):
        # 254 char cap enforced by Field(max_length=254) on EmailStr.
        local = "a" * 250
        too_long = f"{local}@b.co"  # 250 + 1 + 1 + 1 + 2 = 255
        assert len(too_long) > 254
        with pytest.raises(ValidationError):
            SignupRequest(email=too_long, password="p")


@pytest.mark.unit
class TestSignupRequestDisplayName:
    def test_display_name_trimmed(self):
        req = SignupRequest(
            email="a@b.co", password="p", display_name="  Chef  "
        )
        assert req.display_name == "Chef"

    def test_display_name_none_stays_none(self):
        req = SignupRequest(email="a@b.co", password="p", display_name=None)
        assert req.display_name is None

    def test_display_name_default_none(self):
        req = SignupRequest(email="a@b.co", password="p")
        assert req.display_name is None

    def test_display_name_empty_string_rejected(self):
        with pytest.raises(ValidationError):
            SignupRequest(email="a@b.co", password="p", display_name="")

    def test_display_name_whitespace_only_rejected(self):
        with pytest.raises(ValidationError):
            SignupRequest(email="a@b.co", password="p", display_name="   ")

    def test_display_name_max_length_enforced(self):
        too_long = "x" * 101
        with pytest.raises(ValidationError):
            SignupRequest(
                email="a@b.co", password="p", display_name=too_long
            )

    def test_display_name_100_chars_accepted(self):
        ok = "x" * 100
        req = SignupRequest(email="a@b.co", password="p", display_name=ok)
        assert req.display_name == ok

    def test_display_name_unicode_preserved(self):
        req = SignupRequest(
            email="a@b.co", password="p", display_name="Café 👩‍🍳"
        )
        assert req.display_name == "Café 👩‍🍳"


@pytest.mark.unit
class TestSignupRequestPassword:
    def test_password_required(self):
        with pytest.raises(ValidationError):
            SignupRequest(email="a@b.co")  # type: ignore[call-arg]

    def test_password_min_length_one(self):
        # Single char accepted — complexity is FusionAuth's job, not ours.
        req = SignupRequest(email="a@b.co", password="x")
        assert req.password == "x"

    def test_password_empty_rejected(self):
        with pytest.raises(ValidationError):
            SignupRequest(email="a@b.co", password="")

    def test_password_complexity_NOT_enforced_here(self):
        # 'weak' has no digits/symbols/upper; SignupRequest must NOT reject.
        req = SignupRequest(email="a@b.co", password="weak")
        assert req.password == "weak"

    def test_password_with_only_whitespace_accepted_by_schema(self):
        # Schema-level: any non-empty string passes. FA decides complexity.
        req = SignupRequest(email="a@b.co", password="    ")
        assert req.password == "    "


@pytest.mark.unit
class TestSignupRequestRequiredFields:
    def test_email_required(self):
        with pytest.raises(ValidationError):
            SignupRequest(password="p")  # type: ignore[call-arg]

    def test_empty_body_rejected(self):
        with pytest.raises(ValidationError):
            SignupRequest()  # type: ignore[call-arg]

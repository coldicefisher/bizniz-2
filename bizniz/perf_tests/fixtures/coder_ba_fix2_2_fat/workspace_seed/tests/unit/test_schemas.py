import pytest
from pydantic import ValidationError

from app.schemas.auth import (
    UserCreate,
    LoginRequest,
    ResetPasswordRequest,
    Token,
)


@pytest.mark.unit
class TestUserCreateSchema:
    def test_valid_user_create(self):
        user = UserCreate(
            email="user@example.com",
            password="securepass123",
            first_name="John",
            last_name="Doe",
        )
        assert user.email == "user@example.com"
        assert user.password == "securepass123"
        assert user.first_name == "John"
        assert user.last_name == "Doe"
        assert user.phone is None
        assert user.bio is None

    def test_valid_user_create_with_optional_fields(self):
        user = UserCreate(
            email="user@example.com",
            password="securepass123",
            first_name="John",
            last_name="Doe",
            phone="555-1234",
            bio="A test user",
        )
        assert user.phone == "555-1234"
        assert user.bio == "A test user"

    def test_invalid_email_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            UserCreate(
                email="not-an-email",
                password="securepass123",
                first_name="John",
                last_name="Doe",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("email",) for e in errors)

    def test_short_password_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            UserCreate(
                email="user@example.com",
                password="short",
                first_name="John",
                last_name="Doe",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("password",) for e in errors)

    def test_password_exactly_8_chars_accepted(self):
        user = UserCreate(
            email="user@example.com",
            password="12345678",
            first_name="John",
            last_name="Doe",
        )
        assert len(user.password) == 8

    def test_missing_email_rejected(self):
        with pytest.raises(ValidationError):
            UserCreate(
                password="securepass123",
                first_name="John",
                last_name="Doe",
            )

    def test_missing_first_name_rejected(self):
        with pytest.raises(ValidationError):
            UserCreate(
                email="user@example.com",
                password="securepass123",
                last_name="Doe",
            )


@pytest.mark.unit
class TestLoginRequestSchema:
    def test_valid_login_request(self):
        login = LoginRequest(email="user@example.com", password="mypassword")
        assert login.email == "user@example.com"
        assert login.password == "mypassword"

    def test_invalid_email_rejected(self):
        with pytest.raises(ValidationError):
            LoginRequest(email="bad-email", password="mypassword")


@pytest.mark.unit
class TestResetPasswordRequestSchema:
    def test_valid_reset_request(self):
        req = ResetPasswordRequest(token="sometoken", new_password="newpassword123")
        assert req.token == "sometoken"
        assert req.new_password == "newpassword123"

    def test_short_password_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ResetPasswordRequest(token="sometoken", new_password="short")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("new_password",) for e in errors)


@pytest.mark.unit
class TestTokenSchema:
    def test_token_defaults(self):
        token = Token(access_token="abc", refresh_token="def")
        assert token.token_type == "bearer"

    def test_token_custom_type(self):
        token = Token(access_token="abc", refresh_token="def", token_type="custom")
        assert token.token_type == "custom"

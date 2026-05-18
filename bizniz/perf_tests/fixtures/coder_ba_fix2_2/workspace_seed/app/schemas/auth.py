"""Pydantic schemas for auth requests and responses.

Response-side schemas (UserOut, AuthResponse, ErrorResponse) are the
wire-format shapes the auth endpoints return. They MUST NOT contain
any password or password-hash fields — these are output models.

The legacy skeleton schemas below (UserCreate, UserRead, Token,
LoginRequest, etc.) remain because the unmigrated skeleton routes in
``app/api/routes/auth.py`` still import them. They will be removed in
a later milestone once those routes are replaced by the Recipe Box
auth routes (BE-007 ... BE-010).
"""
import re
import uuid
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, ConfigDict, model_validator


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserOut(BaseModel):
    """Public user profile returned by auth endpoints.

    Built from the local ``User`` ORM row via ``from_attributes``.
    NEVER includes password, password hash, or any FA-internal fields.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: Optional[str] = None
    role: str


class AuthResponse(BaseModel):
    """Successful signup/login response: JWT plus the user profile."""

    token: str
    user: UserOut


class ErrorResponse(BaseModel):
    """Structured error envelope returned by auth endpoints.

    ``error`` is a stable machine-readable code (e.g. ``validation_error``,
    ``invalid_credentials``). ``fields`` carries per-field details when
    the failure is a validation error.
    """

    error: str
    fields: Optional[dict] = None


class SignupRequest(BaseModel):
    """Signup request body for POST /api/auth/signup.

    Validates email format (RFC 5322 minimal regex over EmailStr),
    lowercases the email, and trims display_name. Password complexity
    is NOT enforced here — FusionAuth owns the password policy and its
    rejection is translated by the route layer.
    """

    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1)
    display_name: Optional[str] = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> "SignupRequest":
        """Trim display_name, validate email regex, and lowercase email."""
        if self.display_name is not None:
            trimmed = self.display_name.strip()
            if trimmed == "":
                raise ValueError("display_name cannot be empty after trim")
            self.display_name = trimmed

        if not _EMAIL_RE.fullmatch(self.email):
            raise ValueError("email format invalid")

        self.email = self.email.lower()
        return self


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    first_name: str = Field(max_length=100)
    last_name: str = Field(max_length=100)
    phone: Optional[str] = None
    bio: Optional[str] = None
    role: Optional[str] = None  # e.g. "landlord", "tenant" — passed to FusionAuth registration


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    email: str
    first_name: str
    last_name: str
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    is_active: bool
    email_verified: bool = False
    created_at: datetime
    roles: List[str] = []


class UserUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class LoginRequest(BaseModel):
    """Login request body for POST /api/auth/login.

    Validates email format (RFC 5322 minimal regex over EmailStr) and
    lowercases the email before forwarding to FusionAuth. Password is
    required (any non-empty string) — FusionAuth verifies it.
    """

    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1)

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> "LoginRequest":
        """Validate email regex and lowercase email."""
        if not _EMAIL_RE.fullmatch(self.email):
            raise ValueError("email format invalid")

        self.email = self.email.lower()
        return self


class VerifyEmailRequest(BaseModel):
    token: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class OAuthCallbackRequest(BaseModel):
    code: str
    redirect_uri: str

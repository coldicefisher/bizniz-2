"""Auth routes — thin proxies to FusionAuth.

All identity operations (register, login, token refresh, password
reset, email verification) are handled by FusionAuth. These endpoints
proxy requests so the frontend has a single API origin.

The ``/me`` endpoint is the only one that reads the local DB — it
returns the synced User with application-specific fields.
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, get_current_user_with_roles
from app.core.config import get_settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import (
    UserCreate, UserRead, Token, LoginRequest,
    RefreshRequest, VerifyEmailRequest, ResendVerificationRequest,
    ForgotPasswordRequest, ResetPasswordRequest, OAuthCallbackRequest,
)

log = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/auth", tags=["auth"])


async def _fusionauth_request(method: str, path: str, **kwargs) -> httpx.Response:
    """Make a request to FusionAuth's API."""
    url = f"{settings.fusionauth_url}{path}"
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = settings.fusionauth_api_key
    headers.setdefault("Content-Type", "application/json")

    async with httpx.AsyncClient() as client:
        resp = await client.request(method, url, headers=headers, timeout=15.0, **kwargs)
    return resp


def _extract_token(fa_response: dict) -> Token:
    """Extract access/refresh tokens from a FusionAuth response."""
    return Token(
        access_token=fa_response.get("token", ""),
        refresh_token=fa_response.get("refreshToken", ""),
    )


# ── Register ──────────────────────────────────────────────

@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
async def register(data: UserCreate):
    """Register a new user via FusionAuth, then log them in.

    FusionAuth's ``/api/user/registration`` creates the user but the
    response shape varies by tenant config (sometimes a token is
    included, sometimes not). To return a Token reliably we explicitly
    log in after a successful registration.
    """
    reg_payload = {
        "registration": {
            "applicationId": settings.fusionauth_application_id,
            "roles": [data.role] if hasattr(data, "role") and data.role else ["user"],
        },
        "user": {
            "email": data.email,
            "password": data.password,
            "firstName": data.first_name,
            "lastName": data.last_name,
        },
    }
    if data.phone:
        reg_payload["user"]["mobilePhone"] = data.phone

    resp = await _fusionauth_request(
        "POST", "/api/user/registration", json=reg_payload,
    )

    if resp.status_code not in (200, 201):
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        errors = body.get("fieldErrors", {})
        if "user.email" in errors:
            raise HTTPException(status_code=409, detail="Email already registered")
        log.warning(
            "FusionAuth registration failed for %s: status=%s body=%s",
            data.email, resp.status_code, body,
        )
        raise HTTPException(
            status_code=resp.status_code,
            detail=body.get("message") or "Registration failed",
        )

    # Registration succeeded — log in to get a JWT.
    login_resp = await _fusionauth_request("POST", "/api/login", json={
        "applicationId": settings.fusionauth_application_id,
        "loginId": data.email,
        "password": data.password,
    })
    if login_resp.status_code == 200:
        return _extract_token(login_resp.json())
    raise HTTPException(
        status_code=500,
        detail="Registered but login failed; please log in manually",
    )


# ── Login ─────────────────────────────────────────────────

@router.post("/login", response_model=Token)
async def login(data: LoginRequest):
    """Log in via FusionAuth."""
    resp = await _fusionauth_request("POST", "/api/login", json={
        "applicationId": settings.fusionauth_application_id,
        "loginId": data.email,
        "password": data.password,
    })

    if resp.status_code == 200:
        return _extract_token(resp.json())
    if resp.status_code == 404:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if resp.status_code == 423:
        raise HTTPException(status_code=403, detail="Account is locked")

    raise HTTPException(status_code=401, detail="Invalid email or password")


# ── Refresh token ─────────────────────────────────────────

@router.post("/refresh", response_model=Token)
async def refresh(data: RefreshRequest):
    """Refresh tokens via FusionAuth."""
    resp = await _fusionauth_request("POST", "/api/jwt/refresh", json={
        "refreshToken": data.refresh_token,
    })

    if resp.status_code == 200:
        body = resp.json()
        return Token(
            access_token=body.get("token", ""),
            refresh_token=body.get("refreshToken", data.refresh_token),
        )

    raise HTTPException(status_code=401, detail="Invalid or expired refresh token")


# ── Me ────────────────────────────────────────────────────

@router.get("/me", response_model=UserRead)
async def get_me(
    user_and_roles: tuple = Depends(get_current_user_with_roles),
):
    """Return the current user's profile + roles from the JWT.

    Roles come from the JWT claim, never from a local table.
    FusionAuth's claim policy is the source of truth.
    """
    current_user, roles = user_and_roles
    return UserRead(
        user_id=current_user.user_id,
        email=current_user.email,
        first_name=current_user.first_name,
        last_name=current_user.last_name,
        phone=current_user.phone,
        avatar_url=current_user.avatar_url,
        bio=current_user.bio,
        is_active=current_user.is_active,
        email_verified=current_user.email_verified,
        created_at=current_user.created_at,
        roles=roles,
    )


# ── Email verification ───────────────────────────────────

@router.post("/verify-email")
async def verify_email(data: VerifyEmailRequest):
    """Verify email via FusionAuth."""
    resp = await _fusionauth_request(
        "POST", f"/api/user/verify-email/{data.token}",
    )
    if resp.status_code in (200, 202):
        return {"detail": "Email verified successfully"}
    raise HTTPException(status_code=400, detail="Invalid or expired verification token")


@router.post("/resend-verification")
async def resend_verification(data: ResendVerificationRequest):
    """Resend verification email via FusionAuth."""
    resp = await _fusionauth_request("PUT", "/api/user/verify-email", json={
        "email": data.email,
    })
    # Always return success to prevent email enumeration
    return {"detail": "If that email is registered, a verification link has been sent"}


# ── Password reset ────────────────────────────────────────

@router.post("/forgot-password")
async def forgot_password(data: ForgotPasswordRequest):
    """Initiate password reset via FusionAuth."""
    resp = await _fusionauth_request("POST", "/api/user/forgot-password", json={
        "loginId": data.email,
        "applicationId": settings.fusionauth_application_id,
    })
    return {"detail": "If that email is registered, a password reset link has been sent"}


@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest):
    """Complete password reset via FusionAuth."""
    resp = await _fusionauth_request("POST", "/api/user/change-password", json={
        "changePasswordId": data.token,
        "password": data.new_password,
    })
    if resp.status_code == 200:
        return {"detail": "Password reset successfully"}
    raise HTTPException(status_code=400, detail="Invalid or expired reset token")


# ── OAuth (Google) ────────────────────────────────────────

@router.post("/oauth/google", response_model=Token)
async def oauth_google(data: OAuthCallbackRequest):
    """Exchange OAuth code via FusionAuth's identity provider integration."""
    resp = await _fusionauth_request("POST", "/api/identity-provider/login", json={
        "applicationId": settings.fusionauth_application_id,
        "identityProviderId": settings.fusionauth_google_idp_id or "",
        "data": {
            "code": data.code,
            "redirect_uri": data.redirect_uri,
        },
    })
    if resp.status_code == 200:
        return _extract_token(resp.json())
    raise HTTPException(status_code=401, detail="OAuth authentication failed")

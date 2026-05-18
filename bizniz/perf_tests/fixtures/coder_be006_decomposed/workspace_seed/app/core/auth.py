"""FusionAuth-delegated authentication.

All identity operations (register, login, token refresh, password
reset, email verification) are handled by FusionAuth. This module
provides FastAPI dependencies that validate FusionAuth-issued JWTs
and enforce role-based access.

Roles are read from the JWT's ``roles`` claim ONLY. There is no
local fallback to a UserRole table — FusionAuth is the source of
truth, and a local mirror would drift. If the JWT doesn't carry
roles, that's a FusionAuth configuration issue (the access token's
claim policy needs to include roles for the application). Fix it
at FusionAuth, not here.

The two public dependencies — ``get_current_user`` and
``require_roles`` — are the contract downstream code imports.
Engineers never interact with FusionAuth directly; they just use
these as FastAPI ``Depends()``.
"""
import uuid
import logging
from typing import List, Optional

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.models.user import User

log = logging.getLogger(__name__)
settings = get_settings()

# auto_error=False so missing Authorization returns 401 (handled
# explicitly below) instead of FastAPI's default 403.
security = HTTPBearer(auto_error=False)

# Cache for FusionAuth's JWKS public keys (RS256)
_jwks_cache: Optional[dict] = None


async def _get_fusionauth_jwks() -> dict:
    """Fetch FusionAuth's JWKS for JWT verification. Cached after first call."""
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.fusionauth_url}/.well-known/jwks.json",
                timeout=10.0,
            )
            resp.raise_for_status()
            _jwks_cache = resp.json()
            return _jwks_cache
    except Exception as e:
        log.error(f"Failed to fetch FusionAuth JWKS: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        )


def _decode_fusionauth_jwt(token: str, jwks: dict) -> dict:
    """Decode and validate a FusionAuth-issued JWT using its public keys."""
    try:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        rsa_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                rsa_key = key
                break

        if rsa_key is None:
            raise JWTError("No matching key found in JWKS")

        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            audience=settings.fusionauth_application_id,
            issuer=settings.fusionauth_issuer or settings.fusionauth_url,
        )
        return payload
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _sync_user_from_fusionauth(
    fa_user_id: uuid.UUID,
    claims: dict,
    db: AsyncSession,
) -> User:
    """Ensure a local User row exists for the FusionAuth user.

    FusionAuth is the source of truth for identity. We keep a local
    copy for foreign key relationships (a Property belongs to a User,
    a Comment is authored by a User, etc.). This upserts on every
    auth check — cheap, and keeps local data fresh.

    No relationships are eager-loaded because the User model has
    none. Roles are not local; they come from the JWT.
    """
    result = await db.execute(
        select(User).where(User.user_id == fa_user_id)
    )
    user = result.scalar_one_or_none()

    email = claims.get("email", "")
    first_name = claims.get("given_name", claims.get("firstName", ""))
    last_name = claims.get("family_name", claims.get("lastName", ""))

    if user is None:
        user = User(
            user_id=fa_user_id,
            email=email,
            first_name=first_name or "User",
            last_name=last_name or "",
            is_active=True,
            email_verified=claims.get("email_verified", False),
        )
        db.add(user)
        await db.flush()
    else:
        if email and user.email != email:
            user.email = email
        if first_name and user.first_name != first_name:
            user.first_name = first_name
        if last_name and user.last_name != last_name:
            user.last_name = last_name
        user.email_verified = claims.get("email_verified", user.email_verified)

    return user


def _extract_token(
    credentials: Optional[HTTPAuthorizationCredentials],
) -> str:
    """Return the bearer token, or raise 401 if missing.

    HTTPBearer is configured with ``auto_error=False`` so we can raise
    401 (not 403) when the Authorization header is absent — the latter
    is FastAPI's default and surprises every integration test that
    expects "missing credentials" to be 401.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


# ── Public dependencies (the contract) ───────────────────

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency: validate FusionAuth JWT and return the local User.

    Usage:
        @router.get("/protected")
        async def endpoint(user: User = Depends(get_current_user)):
            ...
    """
    token = _extract_token(credentials)
    jwks = await _get_fusionauth_jwks()
    claims = _decode_fusionauth_jwt(token, jwks)

    user_id_str = claims.get("sub")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject claim",
        )

    try:
        fa_user_id = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user ID in token",
        )

    user = await _sync_user_from_fusionauth(fa_user_id, claims, db)
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is deactivated",
        )
    return user


def require_roles(*allowed_roles: str):
    """Dependency factory: restrict access to users with specific roles.

    Roles are read from the FusionAuth JWT claims (``roles`` array
    in the application-scoped registration). If the JWT doesn't
    include the role, the request is rejected with 403 — there is
    no local fallback. FusionAuth's claim policy must include
    roles for this to work; the spec-driven kickstart configures
    that automatically.

    Usage:
        @router.get("/admin", dependencies=[Depends(require_roles("admin"))])
        async def admin_endpoint(): ...

        @router.get("/admin")
        async def admin_endpoint(user: User = Depends(require_roles("admin"))): ...
    """
    async def _check_roles(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        token = _extract_token(credentials)
        jwks = await _get_fusionauth_jwks()
        claims = _decode_fusionauth_jwt(token, jwks)

        user_id_str = claims.get("sub")
        if not user_id_str:
            raise HTTPException(status_code=401, detail="Token missing subject")

        fa_user_id = uuid.UUID(user_id_str)
        user = await _sync_user_from_fusionauth(fa_user_id, claims, db)

        if not user.is_active:
            raise HTTPException(status_code=401, detail="Account deactivated")

        jwt_roles = set(claims.get("roles", []))
        if not jwt_roles.intersection(allowed_roles):
            log.warning(
                "Access denied for user %s on roles=%s — JWT had roles=%s",
                user.email, allowed_roles, sorted(jwt_roles),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user
    return _check_roles


def get_jwt_roles(claims: dict) -> List[str]:
    """Helper: return the role names from a decoded JWT claim payload.

    Useful in routes that want to surface "what roles does this user
    have" without re-validating the token (e.g. /api/v1/auth/me).
    """
    return list(claims.get("roles") or [])


async def get_current_user_with_roles(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> tuple:
    """Like ``get_current_user``, but also returns the JWT roles.

    Returns ``(user, roles)`` so routes that need to render role lists
    (e.g. ``/me``) don't have to redecode the token themselves. Roles
    come from the JWT claim — same source of truth as ``require_roles``.
    """
    token = _extract_token(credentials)
    jwks = await _get_fusionauth_jwks()
    claims = _decode_fusionauth_jwt(token, jwks)

    user_id_str = claims.get("sub")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject claim",
        )

    try:
        fa_user_id = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user ID in token",
        )

    user = await _sync_user_from_fusionauth(fa_user_id, claims, db)
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is deactivated",
        )

    return user, get_jwt_roles(claims)

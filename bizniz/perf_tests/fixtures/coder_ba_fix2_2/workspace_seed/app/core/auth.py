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
import asyncio
import uuid
import logging
from typing import Callable, List, Optional
from uuid import UUID

import httpx
from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.models.user import User
from app.repositories.user_repository import (
    DuplicateEmailInMirror,
    get_user_by_id,
    upsert_user_mirror,
)
from app.services import fusionauth_client
from app.services.fusionauth_client import FusionAuthUnavailable

log = logging.getLogger(__name__)
settings = get_settings()

# Cache for FusionAuth's JWKS public keys (RS256). Populated on
# first call to ``_get_jwks_with_refresh`` and refreshed once on a
# kid-miss (key rotation). ``None`` means "cold cache — never
# fetched". A populated dict is "warm" — even if every key inside
# is stale, the cache itself is considered warm and a refresh
# failure during kid-miss falls through to invalid_token rather
# than 503'ing the whole API. See ``_get_jwks_with_refresh`` for
# the cold-vs-warm contract.
_jwks_cache: Optional[dict] = None

# Serializes concurrent JWKS fetches / refreshes so N parallel
# kid-misses don't N-fanout HTTP calls to FusionAuth. Async lock
# because ``get_current_user`` is an async dependency and the
# underlying ``fusionauth_client.get_jwks`` is an ``async`` call.
_jwks_lock: asyncio.Lock = asyncio.Lock()

# Role precedence: highest-privilege first. ``_pick_role`` iterates
# this tuple and returns the first role present in the JWT's roles
# claim. Order is the authorization contract — do NOT reorder.
_ROLE_PRECEDENCE: tuple[str, ...] = ("super_admin", "admin", "user")


def _pick_role(roles: list[str]) -> Optional[str]:
    """Return the highest-precedence role present in ``roles``.

    Walks ``_ROLE_PRECEDENCE`` in order and returns the first role
    that appears in the input list. Returns ``None`` if none of the
    known roles are present (e.g. JWT carries only unknown roles or
    an empty list). Callers treat ``None`` as "unauthenticated for
    role-gated operations".
    """
    role_set = set(roles or [])
    for role in _ROLE_PRECEDENCE:
        if role in role_set:
            return role
    return None


class CurrentUser(BaseModel):
    """Authenticated user context exposed to route handlers.

    Populated by ``get_current_user`` after JWT validation and local
    mirror lookup. ``role`` is derived from the JWT's roles claim via
    ``_pick_role`` — the JWT is authoritative for authorization;
    the local mirror's ``role`` column is a snapshot only.
    """

    id: UUID
    email: str
    display_name: Optional[str] = None
    role: str


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


def _jwks_contains_kid(jwks: Optional[dict], kid: str) -> bool:
    """Return True iff ``jwks`` has a key whose ``kid`` matches.

    Defensive against a missing or malformed ``keys`` list — a JWKS
    document without a ``keys`` array is treated as "kid not present"
    rather than crashing the validator. The signature-verification
    step downstream will surface the real error as ``invalid_token``.
    """
    if not jwks:
        return False
    for key in jwks.get("keys", []) or []:
        if key.get("kid") == kid:
            return True
    return False


async def _get_jwks_with_refresh(kid: str) -> dict:
    """Return JWKS, refreshing on a kid-miss.

    Behavior:
      * **Cold cache** (``_jwks_cache is None``) — fetch via
        :func:`fusionauth_client.get_jwks`, store, and return. If
        FusionAuth raises :class:`FusionAuthUnavailable` here, the
        exception PROPAGATES; the caller translates it to HTTP 503
        ``auth_service_unavailable`` because we have no fallback
        keys at all.
      * **Warm cache, kid present** — return cached JWKS unchanged.
        No HTTP call.
      * **Warm cache, kid missing** — refresh ONCE by re-calling
        ``get_jwks()`` and overwriting the cache, then return. If
        FusionAuth is unavailable during this refresh, SWALLOW the
        exception and return the stale cache. The subsequent
        signature check will fail with ``invalid_token`` for the
        missing kid — the correct user-facing signal for "this
        token's key isn't recognised" — instead of 503'ing the
        whole API because FA happened to blip during a key
        rotation.

    The fetch / refresh is serialized through ``_jwks_lock`` so N
    concurrent kid-miss requests issue at most one HTTP call to
    FusionAuth. After acquiring the lock, the cold-cache and
    kid-presence checks are re-run so the second waiter through
    the lock sees the work the first waiter already did.
    """
    global _jwks_cache

    if _jwks_cache is not None and _jwks_contains_kid(_jwks_cache, kid):
        return _jwks_cache

    async with _jwks_lock:
        # Re-check under the lock — another waiter may have just
        # populated or refreshed the cache while we were queued.
        if _jwks_cache is None:
            # Cold cache: any FA failure is fatal — propagate so the
            # dependency translates to 503 auth_service_unavailable.
            fresh = await fusionauth_client.get_jwks()
            _jwks_cache = fresh
            return _jwks_cache

        if _jwks_contains_kid(_jwks_cache, kid):
            return _jwks_cache

        # Warm cache + kid miss: refresh once. If FA blips, fall
        # through to the stale cache so the signature check fails
        # with invalid_token rather than 503'ing the whole API.
        try:
            fresh = await fusionauth_client.get_jwks()
            _jwks_cache = fresh
        except FusionAuthUnavailable as exc:
            log.warning(
                "jwks_refresh_failed_falling_back_to_stale_cache "
                "kid=%s err=%s",
                kid, exc,
            )
        return _jwks_cache


def _reset_jwks_cache_for_tests() -> None:
    """Reset the module-level JWKS cache to ``None``.

    Test-only hermeticity helper — clears the in-process cache
    between tests that exercise the cold-cache code path. NOT a
    public API; routers must not call it (would force every
    in-flight request to refetch JWKS in lockstep). The
    ``_for_tests`` suffix is the warning.
    """
    global _jwks_cache
    _jwks_cache = None


def _validate_token_shape(authorization: str) -> str:
    """Validate the Authorization header shape and return the bare token.

    Performs cheap, signature-free structural checks BEFORE any
    cryptographic work — get rid of obviously bad input fast so the
    JWKS fetch and RS256 verification only ever see plausible tokens.

    Checks, in order:
      1. Header is non-empty and starts with the ``Bearer`` scheme
         (case-INsensitive — RFC 6750 §2.1 says the scheme token is
         matched case-insensitively, so ``bearer``/``BEARER``/``Bearer``
         are all valid). Missing / wrong scheme → 401
         ``{'error': 'unauthenticated'}``.
      2. Token after the prefix (stripped of whitespace) is non-empty.
         Empty → 401 ``{'error': 'unauthenticated'}``.
      3. Token has exactly three ``'.'``-separated segments (header,
         payload, signature). Wrong count → 401
         ``{'error': 'invalid_token'}``.

    The 401 codes are distinct on purpose: the SPA shows different UX
    for "you forgot to log in" vs "your token is corrupt".
    """
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated"},
        )
    # RFC 6750: the scheme name is case-insensitive but MUST be followed
    # by whitespace. Strip a single leading scheme token + its separator
    # rather than stripping a fixed-case prefix.
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer" or not rest:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated"},
        )
    token = rest.strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated"},
        )
    if len(token.split(".")) != 3:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token"},
        )
    return token


def _decode_unverified_header(token: str) -> dict:
    """Decode the JWT header without verifying the signature and pin alg=RS256.

    Two checks happen here, BOTH before any signature verification:

      1. The header parses as a valid JWT header via
         ``jose.jwt.get_unverified_header``. Garbage → 401
         ``{'error': 'invalid_token'}``.
      2. The ``alg`` claim is literally ``'RS256'``. Anything else —
         including ``'none'``, ``'HS256'``, ``'HS384'``, ``'HS512'`` —
         is rejected with a WARN log and 401 ``{'error':
         'invalid_token'}``.

    Pinning algorithm BEFORE signature verification defeats the
    classic ``alg=none`` and "HS256-with-RSA-public-key-as-secret"
    attacks: an attacker who can choose the algorithm advertised in
    the header can otherwise downgrade RS256 to HS256 and present a
    forged token signed with the (public!) RSA key.
    """
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token"},
        )
    if header.get("alg") != "RS256":
        log.warning("unexpected_alg", extra={"alg": header.get("alg")})
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token"},
        )
    return header


async def _verify_jwt_signature_and_claims(token: str, header: dict) -> dict:
    """Verify the JWT signature and standard claims; return decoded payload.

    Pipeline (each step is independent — failure surfaces a specific
    user-facing error code so the SPA can distinguish "session expired,
    please log in again" from a generic "auth failed"):

      1. **Extract ``kid``** from the (already alg-pinned) unverified
         header. Missing ``kid`` → 401 ``{'error': 'invalid_token'}``.
      2. **Fetch JWKS** via :func:`_get_jwks_with_refresh`, which
         handles the cold-cache fetch + warm-cache refresh-on-kid-miss
         contract. A cold-cache :class:`FusionAuthUnavailable` is
         translated to 503 ``{'error': 'auth_service_unavailable'}``
         — we have no fallback keys, so we cannot validate the token.
         (A warm-cache refresh failure is swallowed inside the helper
         and falls through to the kid-miss path below.)
      3. **Find the matching JWK** by ``kid``. Not found → 401
         ``{'error': 'invalid_token'}`` with a WARN log. This is the
         expected outcome when FA blipped during a key rotation and
         the stale cache no longer carries the new kid.
      4. **Decode + verify** with :func:`jose.jwt.decode`, pinned to
         ``algorithms=['RS256']`` (defense-in-depth — even though
         ``_decode_unverified_header`` already pinned alg before this
         function ran, ``decode`` must not trust the header). Claims
         are required:

           * ``aud`` must equal ``settings.fusionauth_application_id``.
           * ``iss`` must equal ``settings.fusionauth_issuer``.
           * ``exp`` must be present and in the future (with
             ``settings.jwt_leeway_seconds`` of clock-skew tolerance).
           * ``nbf`` is auto-checked by ``jose`` when present — no
             extra code needed.

      5. **Exception mapping** translates the python-jose error
         hierarchy into the public auth error vocabulary:

           * :class:`ExpiredSignatureError` → 401
             ``{'error': 'token_expired'}`` — distinct code so the SPA
             can attempt a silent re-login flow.
           * :class:`JWTClaimsError` (wrong aud / iss / etc.) → 401
             ``{'error': 'invalid_token'}``.
           * Any other :class:`JWTError` (bad signature, malformed
             payload, etc.) → 401 ``{'error': 'invalid_token'}``.

    Note on ``leeway`` placement: python-jose 3.x accepts ``leeway``
    inside the ``options`` dict (NOT as a top-level kwarg). The
    ``options`` dict here is the canonical place for both the
    ``require_*`` flags and the leeway value.
    """
    kid = header.get("kid")
    if not kid:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token"},
        )

    try:
        jwks = await _get_jwks_with_refresh(kid)
    except FusionAuthUnavailable:
        raise HTTPException(
            status_code=503,
            detail={"error": "auth_service_unavailable"},
        )

    key = next(
        (k for k in jwks.get("keys", []) or [] if k.get("kid") == kid),
        None,
    )
    if key is None:
        log.warning("jwks_kid_not_found", extra={"kid": kid})
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token"},
        )

    try:
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.fusionauth_application_id,
            issuer=settings.fusionauth_issuer,
            options={
                "leeway": settings.jwt_leeway_seconds,
                "require_aud": True,
                "require_iss": True,
                "require_exp": True,
            },
        )
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail={"error": "token_expired"},
        )
    except JWTClaimsError:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token"},
        )
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token"},
        )


# ── Public dependencies (the contract) ───────────────────

async def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """FastAPI dependency: validate a FusionAuth JWT and return the caller's context.

    Pipeline (each step delegates to the focused helper that owns it,
    so the error vocabulary stays consistent across the auth surface):

      1. ``_validate_token_shape`` — strip ``Bearer ``, assert non-empty
         + three-segment shape. Missing/malformed header → 401
         ``unauthenticated`` / ``invalid_token``.
      2. ``_decode_unverified_header`` — parse the JWT header and pin
         ``alg=RS256`` BEFORE signature verification (defeats the
         ``alg=none`` / HS256-with-public-key downgrade attacks).
      3. ``_verify_jwt_signature_and_claims`` — JWKS-backed RS256
         verification with required ``aud`` / ``iss`` / ``exp`` claims.
         Cold-cache JWKS unavailability surfaces as 503
         ``auth_service_unavailable``; everything else as 401.
      4. **sub claim** — required, must parse as UUID. Either failure
         → 401 ``invalid_token`` (NOT ``unauthenticated`` — the caller
         did present a credential, it just wasn't usable).
      5. **roles claim** — FusionAuth puts the application-scoped roles
         as a top-level ``roles`` list on the access token (see
         AUTH_CONTRACT.md). Missing / empty / unknown-only → 403
         ``no_role_assigned``. Role precedence (super_admin > admin >
         user) comes from ``_pick_role`` and is the authoritative
         source for downstream authz checks — NOT the local mirror's
         ``role`` column, which is just a snapshot for diagnostics.
      6. **Local mirror lookup** — ``get_user_by_id``. If absent, the
         row is auto-created via ``upsert_user_mirror`` from the JWT
         claims (``email``, optional ``name`` / ``preferred_username``
         for display_name), defaulting ``role='user'`` (the mirror
         column is informational; JWT roles win for authz). The
         autocreate is logged at INFO with ``mirror_autocreated``.
         ``DuplicateEmailInMirror`` → 500
         ``duplicate_email_in_mirror`` (pathological: two FA user
         IDs collided on the same email). Any other DB exception
         bubbles so the global handler can translate to 503
         ``database_unavailable``.
      7. **Return** a :class:`CurrentUser` with role taken from the
         JWT precedence pick (NOT ``user.role``).
    """
    token = _validate_token_shape(authorization)
    header = _decode_unverified_header(token)
    claims = await _verify_jwt_signature_and_claims(token, header)

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token"},
        )
    try:
        sub_uuid = UUID(sub)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token"},
        )

    roles_claim = claims.get("roles")
    if not roles_claim:
        raise HTTPException(
            status_code=403,
            detail={"error": "no_role_assigned"},
        )
    role = _pick_role(roles_claim)
    if role is None:
        raise HTTPException(
            status_code=403,
            detail={"error": "no_role_assigned"},
        )

    user = await get_user_by_id(db, sub_uuid)
    if user is None:
        # Bridge the shared synchronous ``upsert_user_mirror`` onto the
        # AsyncSession via ``run_sync`` — same pattern as the BA-fix1-1
        # repair in auth_signup / auth_login. Calling the helper directly
        # against an AsyncSession would return an un-awaited coroutine
        # from the inner ``session.execute(stmt)`` and crash on
        # ``.scalar_one_or_none()``.
        email = claims.get("email", "")
        display_name = claims.get("name") or claims.get("preferred_username")
        try:
            user = await db.run_sync(
                lambda s: upsert_user_mirror(
                    s,
                    fa_user_id=sub_uuid,
                    email=email,
                    role="user",
                    display_name=display_name,
                )
            )
        except DuplicateEmailInMirror:
            await db.rollback()
            raise HTTPException(
                status_code=500,
                detail={"error": "duplicate_email_in_mirror"},
            )
        await db.commit()
        log.info("mirror_autocreated", extra={"user_id": str(sub_uuid)})

    return CurrentUser(
        id=sub_uuid,
        email=user.email,
        display_name=user.display_name,
        role=role,
    )


def require_roles(*allowed: str | list[str] | tuple[str, ...]) -> Callable:
    """Dependency factory: restrict access to callers whose role is in ``allowed``.

    Composes with :func:`get_current_user` — the inner checker takes
    the already-validated :class:`CurrentUser` and only enforces the
    role gate, so JWT validation, JWKS fetch, and local-mirror upsert
    are not duplicated.

    Accepts either varargs or a single list/tuple for ergonomic parity
    with both call styles seen in the codebase::

        Depends(require_roles("admin"))                # varargs
        Depends(require_roles("admin", "super_admin")) # varargs
        Depends(require_roles(["admin"]))              # single list (issue spec)

    If ``current_user.role`` is not in the resolved allowed set, raises
    ``HTTPException(403, detail={'error': 'forbidden'})``. Otherwise
    returns the ``CurrentUser`` so handlers can write::

        async def admin_endpoint(
            user: CurrentUser = Depends(require_roles(["admin"])),
        ): ...
    """
    if len(allowed) == 1 and isinstance(allowed[0], (list, tuple)):
        allowed_set = set(allowed[0])
    else:
        allowed_set = set(allowed)  # type: ignore[arg-type]

    async def _checker(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        """Reject if ``current_user.role`` is not in the allowed set."""
        if current_user.role not in allowed_set:
            raise HTTPException(
                status_code=403,
                detail={"error": "forbidden"},
            )
        return current_user

    return _checker


def get_jwt_roles(claims: dict) -> List[str]:
    """Helper: return the role names from a decoded JWT claim payload.

    Useful in routes that want to surface "what roles does this user
    have" without re-validating the token (e.g. /api/v1/auth/me).
    """
    return list(claims.get("roles") or [])



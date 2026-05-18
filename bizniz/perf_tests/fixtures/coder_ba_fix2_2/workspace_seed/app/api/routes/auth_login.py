"""POST /api/v1/auth/login route — module scaffold (BE-008-U1).

Auto-mounted by ``app/main.py`` under ``settings.api_v1_prefix``
(``/api/v1``), so the router below declares ``prefix='/auth'`` and the
final URL is ``/api/v1/auth/login``. Match the BE-007 convention in
``app/api/routes/auth_signup.py``: declare only ``/auth`` because the
auto-mount adds ``/api/v1``. Declaring ``/api/auth`` here would
double-prefix to ``/api/v1/api/auth/login``.

This file is the BE-008 scaffold layer: imports, router, and a small
helper that builds a ``UserOut`` from the local mirror row + the
JWT-derived role. The actual route handler lands in BE-008-U2.

JWT validation reuse
--------------------
Step 5 of the BE-008 spec says: validate FA's returned JWT using the
SAME helper the auth middleware uses, so the algorithm-pinning and
JWKS-cache invariants set by BE-006 are not bypassed. The helper that
performs RS256 signature + aud/iss/exp verification is
``app.core.auth._verify_jwt_signature_and_claims`` — module-private
(leading underscore) in BE-006. We import it here with this comment
acknowledging the layering exception: U2 will call it after first
decoding the header via ``_decode_unverified_header`` (which pins
alg=RS256 BEFORE signature verification, defeating the alg=none /
HS256-with-public-key downgrade attacks). Re-implementing JWT parsing
in this route would silently lose those invariants — DO NOT.
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.auth import (
    LoginRequest,
    AuthResponse,
    UserOut,
    ErrorResponse,
)
from app.services import fusionauth_client
from app.services.fusionauth_client import (
    FusionAuthValidationError,
    FusionAuthUnavailable,
)
from app.repositories.user_repository import (
    get_user_by_id,
    upsert_user_mirror,
    DuplicateEmailInMirror,
)
from app.db.session import get_db

# Layering exception: BE-006's JWT-validation helpers are module-private
# in app.core.auth (leading underscore). The BE-008 login route MUST
# reuse them — re-implementing JWT parsing here would bypass the
# algorithm-pinning + JWKS-cache invariants and is a security regression.
from app.core.auth import (
    _decode_unverified_header,
    _verify_jwt_signature_and_claims,
    _pick_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _build_user_out_from_claims(user_row, jwt_role: str) -> UserOut:
    """Build a ``UserOut`` from the local mirror row + the JWT-derived role.

    The mirror row supplies ``id``, ``email``, and ``display_name`` —
    the stable fields the SPA renders. ``role`` is taken from the JWT
    (passed in as ``jwt_role``) and NOT from ``user_row.role``: BE-006's
    auth contract makes the JWT the authoritative source of authorization,
    and the mirror's ``role`` column is only a snapshot for diagnostics.
    Using ``user_row.role`` here would risk handing the SPA a stale role
    if FusionAuth changed the user's roles after the mirror row was
    written.
    """
    return UserOut(
        id=user_row.id,
        email=user_row.email,
        display_name=user_row.display_name,
        role=jwt_role,
    )


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    response_model=AuthResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """Exchange email + password for a FusionAuth JWT.

    Flow (locked by spec — user-enumeration defense in particular):

    1. Pydantic ``LoginRequest`` already trimmed/lowercased email and
       enforced non-empty fields; FastAPI returns 422 on validation
       failure via the skeleton's RequestValidationError handler.
    2. POST to FusionAuth ``/api/login`` via the typed client.
    3. ALL FA 4xx (404 unknown user, 401 wrong password, 423 locked,
       400 malformed) map to an IDENTICAL 401 ``invalid_credentials``
       response — same status, same body — to prevent user enumeration
       and lock-state leakage to unauthenticated callers. Logged at
       INFO (normal traffic), with email but never password.
    4. FA 5xx / network error → 503 ``auth_service_unavailable``.
    5. Validate the FA-issued JWT via the SAME helpers the auth
       middleware uses (BE-006's ``_decode_unverified_header`` +
       ``_verify_jwt_signature_and_claims``) so algorithm-pinning and
       JWKS-cache invariants are preserved. A signature / claim error
       from FA's own token means FA misconfiguration → 502
       ``auth_token_invalid``. A 503 ``auth_service_unavailable`` from
       the helper (cold JWKS cache + FA blip) is preserved as 503.
    6. Extract ``sub`` (must parse as UUID).
    7. Extract ``roles`` and pick highest-precedence via
       :func:`_pick_role`. Empty / unknown-only → 403
       ``no_role_assigned``.
    8. Look up the local mirror row by FA user id. If missing
       (legacy / seeded FA users that never went through signup),
       auto-create from JWT claims with ``role='user'`` (mirror role
       is informational; JWT is authoritative for authz).
    9. Return 200 with the JWT and ``UserOut`` (role from JWT pick).

    Password is NEVER logged: only ``payload.email`` and ids appear in
    the ``extra={...}`` dicts. The only mention of ``password`` in this
    function is the FA call argument.
    """
    try:
        fa_resp = await fusionauth_client.login(
            email=payload.email,
            password=payload.password,
        )
    except FusionAuthValidationError as e:
        # ALL FA 4xx — 404 unknown user, 401 wrong password, 423 locked,
        # 400 malformed — map to the SAME 401 invalid_credentials shape.
        # Distinguishing would leak account existence / lock state.
        logger.info(
            "fa_login_rejected",
            extra={"email": payload.email, "fa_status": e.status_code},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_credentials"},
        )
    except FusionAuthUnavailable:
        logger.error(
            "fa_unavailable_on_login",
            extra={"email": payload.email},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "auth_service_unavailable"},
        )

    token = fa_resp.get("token") if isinstance(fa_resp, dict) else None
    if not token:
        logger.error(
            "fa_login_response_missing_token",
            extra={"email": payload.email},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "auth_token_invalid"},
        )

    try:
        header = _decode_unverified_header(token)
        claims = await _verify_jwt_signature_and_claims(token, header)
    except HTTPException as http_exc:
        inner_detail = http_exc.detail if isinstance(http_exc.detail, dict) else {}
        # Cold-JWKS + FA blip: preserve the 503 because we genuinely
        # cannot validate the token. Everything else (bad signature,
        # bad claims, expired, etc.) on a FA-just-issued token means FA
        # misconfiguration — surface as 502 auth_token_invalid.
        if inner_detail.get("error") == "auth_service_unavailable":
            raise
        logger.error(
            "fa_returned_invalid_jwt",
            extra={
                "email": payload.email,
                "inner_status": http_exc.status_code,
                "inner_detail": http_exc.detail,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "auth_token_invalid"},
        )

    sub = claims.get("sub")
    if not sub:
        logger.error(
            "fa_jwt_missing_sub",
            extra={"email": payload.email},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "auth_token_invalid"},
        )
    try:
        sub_uuid = UUID(str(sub))
    except (ValueError, TypeError):
        logger.error(
            "fa_jwt_sub_not_uuid",
            extra={"email": payload.email},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "auth_token_invalid"},
        )

    roles = claims.get("roles") or []
    if not roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "no_role_assigned"},
        )
    picked_role = _pick_role(roles)
    if picked_role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "no_role_assigned"},
        )

    user_row = await get_user_by_id(db, sub_uuid)
    if user_row is None:
        fa_email = claims.get("email") or payload.email
        display_name = claims.get("name") or claims.get("preferred_username")
        try:
            user_row = await db.run_sync(
                lambda s: upsert_user_mirror(
                    s,
                    fa_user_id=sub_uuid,
                    email=fa_email,
                    role="user",
                    display_name=display_name,
                )
            )
            await db.commit()
        except DuplicateEmailInMirror:
            await db.rollback()
            logger.error(
                "login_mirror_duplicate_email",
                extra={"fa_user_id": str(sub_uuid)},
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "duplicate_email_in_mirror"},
            )
        logger.info(
            "mirror_autocreated_on_login",
            extra={"user_id": str(sub_uuid)},
        )

    return AuthResponse(
        token=token,
        user=_build_user_out_from_claims(user_row, jwt_role=picked_role),
    )

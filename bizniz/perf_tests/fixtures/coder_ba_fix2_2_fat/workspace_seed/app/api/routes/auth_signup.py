"""POST /api/v1/auth/signup route — module scaffold.

Auto-mounted by ``app/main.py`` under ``settings.api_v1_prefix``
(``/api/v1``), so the router below declares ``prefix='/auth'`` and the
final URL is ``/api/v1/auth/signup``. The skeleton convention (see
``app/api/routes/auth.py``) is to NOT include ``/api/v1`` in the prefix
— the auto-mount adds it. Adding ``/api/v1`` (or ``/api/auth``) here
would double-prefix.

This file is the BE-007 scaffold layer: imports, router, and a pure
FusionAuth→HTTPException translation helper. The actual route handler
is added in BE-007-U2; tests live alongside in BE-007-U3.
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.auth import (
    SignupRequest,
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
    upsert_user_mirror,
    DuplicateEmailInMirror,
)
from app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _redact_signup_body(body: object) -> object:
    """Return a copy of an FA error body with password-like fields elided.

    Mirrors the redaction the FusionAuth client does on ``__str__`` so
    the body can be safely written to module logs without leaking a
    plaintext password that an error envelope might echo.
    """
    redact_keys = {"password", "currentPassword", "newPassword"}
    if isinstance(body, dict):
        return {
            k: ("***" if k in redact_keys else _redact_signup_body(v))
            for k, v in body.items()
        }
    if isinstance(body, list):
        return [_redact_signup_body(item) for item in body]
    return body


def _translate_fa_signup_error(exc: FusionAuthValidationError) -> HTTPException:
    """Translate a FusionAuth validation error into the right HTTPException.

    Inspects ``exc.body`` (FA's ``{"fieldErrors": {...}}`` envelope) and
    returns the user-facing HTTPException the signup route should raise:

    * ``fieldErrors.user.password.*`` → 400 weak_password with the
      password fields surfaced to the caller (frontend renders them).
    * ``fieldErrors['[duplicate]user.email']`` → 409 email_already_registered.
    * ``fieldErrors['[duplicate]registration']`` → 500 auth_config_error
      and an ERROR log — this is the path-arg-vs-no-path-arg pitfall
      from the auth contract; the user did nothing wrong.
    * any other validation error → 400 validation_error with empty
      ``fields`` and a WARNING log carrying a redacted body so the
      mapping can be expanded later without leaking secrets.

    The contract documents FA's exact key shapes — ``user.password``
    (dotted) for weak-password, ``[duplicate]<field>`` for uniqueness
    violations. Both shapes are dialect-specific to FA's tenant default
    schema; see AUTH_CONTRACT.md.

    Pure function (no I/O beyond ``logger.error`` / ``logger.warning``)
    so the BE-007-U3 unit tests can exercise each mapping without
    spinning up FastAPI or FusionAuth.
    """
    body = exc.body if isinstance(exc.body, dict) else {}
    field_errors = body.get("fieldErrors", {}) if isinstance(body, dict) else {}
    if not isinstance(field_errors, dict):
        field_errors = {}

    password_keys = [k for k in field_errors.keys() if k.startswith("user.password")]
    if password_keys:
        password_messages: list = []
        for k in password_keys:
            val = field_errors.get(k, [])
            if isinstance(val, list):
                password_messages.extend(val)
            else:
                password_messages.append(val)
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "weak_password",
                "fields": {"password": password_messages},
            },
        )

    duplicate_email_keys = [
        k for k in field_errors.keys()
        if k.startswith("[duplicate]") and "user.email" in k
    ]
    if duplicate_email_keys:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "email_already_registered"},
        )

    duplicate_registration_keys = [
        k for k in field_errors.keys()
        if k.startswith("[duplicate]") and "registration" in k
    ]
    if duplicate_registration_keys:
        logger.error(
            "fa_config_error_duplicate_registration: FA rejected /api/user/registration "
            "with [duplicate]registration — this is the path-arg-vs-no-path-arg pitfall. "
            "redacted_body=%r",
            _redact_signup_body(body),
        )
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "auth_config_error"},
        )

    logger.warning(
        "fa_signup_validation_error_unmapped: redacted_body=%r",
        _redact_signup_body(body),
    )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "validation_error", "fields": {}},
    )


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    response_model=AuthResponse,
    responses={
        400: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def signup(
    payload: SignupRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """Register a new user in FusionAuth, mirror locally, and return a JWT.

    Flow (locked by spec — reordering changes failure semantics):

    1. Pydantic already validated the payload at the framework layer.
       Missing or malformed fields surface as 422 via FastAPI's
       ``RequestValidationError`` handler (skeleton-provided); password
       complexity is enforced by FusionAuth, not here.
    2. Register the user in FusionAuth with ``roles=['user']``
       **hardcoded**. Defense in depth: ``SignupRequest`` does not have
       a ``role`` field, but even if a future revision adds one, this
       route ignores it. A public signup endpoint must never let the
       caller request ``admin`` / ``super_admin``.
    3. On FA failure, translate via :func:`_translate_fa_signup_error`
       (400 weak_password / 409 duplicate / 500 config / 400 unmapped)
       or surface as 503 if FA is unreachable. Do NOT write to the local
       mirror if FA registration failed.
    4. Parse FA's ``user.id`` as a UUID. Missing or non-UUID → 500
       ``auth_config_error`` and an ERROR log — this means FA returned
       a response shape the contract didn't anticipate.
    5. Insert into the local mirror. On any failure the FA user already
       exists upstream; orphan reconciliation is deferred per spec, so
       we log the ``fa_user_id`` at ERROR level and surface 500
       ``user_mirror_failed`` without deleting from FA.
    6. Login the freshly created user to obtain a JWT (FA is
       authoritative — we never mint tokens locally). FA down → 503;
       FA validation error post-create → 500 auth_config_error (this
       indicates the just-created credentials don't authenticate, which
       is a backend bug).
    7. Return 201 with the JWT and the public ``UserOut`` projection.

    The ``extra={...}`` dicts in every ``logger.error`` call carry only
    ``email`` and ``fa_user_id`` — never ``payload.password`` or any
    other field that might contain it.
    """
    try:
        fa_user = await fusionauth_client.register_user(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            roles=["user"],
        )
    except FusionAuthValidationError as e:
        raise _translate_fa_signup_error(e)
    except FusionAuthUnavailable:
        logger.error(
            "fa_unavailable_on_signup",
            extra={"email": payload.email},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "auth_service_unavailable"},
        )

    fa_user_id_str = (
        fa_user.get("user", {}).get("id") if isinstance(fa_user, dict) else None
    )
    if not fa_user_id_str:
        logger.error(
            "fa_register_missing_user_id",
            extra={"email": payload.email},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "auth_config_error"},
        )
    try:
        fa_user_id_uuid = UUID(str(fa_user_id_str))
    except (ValueError, TypeError):
        logger.error(
            "fa_register_invalid_user_id",
            extra={"email": payload.email},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "auth_config_error"},
        )

    try:
        user_row = await db.run_sync(
            lambda s: upsert_user_mirror(
                s,
                fa_user_id=fa_user_id_uuid,
                email=payload.email,
                role="user",
                display_name=payload.display_name,
            )
        )
        await db.commit()
    except DuplicateEmailInMirror:
        # ``upsert_user_mirror`` already rolled the sync-proxy transaction
        # back inside ``run_sync``; mirror it on the AsyncSession so any
        # subsequent ``db`` use in this request finds a clean state.
        await db.rollback()
        logger.error(
            "user_mirror_duplicate_email_collision",
            extra={
                "fa_user_id": str(fa_user_id_uuid),
                "email": payload.email,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "email_already_registered"},
        )
    except Exception as e:
        await db.rollback()
        logger.error(
            "user_mirror_failed",
            extra={
                "fa_user_id": str(fa_user_id_uuid),
                "error_type": type(e).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "user_mirror_failed"},
        )

    try:
        login_resp = await fusionauth_client.login(
            email=payload.email,
            password=payload.password,
        )
    except FusionAuthUnavailable:
        logger.error(
            "fa_login_unavailable_post_signup",
            extra={"fa_user_id": str(fa_user_id_uuid)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "auth_service_unavailable"},
        )
    except FusionAuthValidationError as e:
        logger.error(
            "fa_login_failed_post_signup",
            extra={
                "fa_user_id": str(fa_user_id_uuid),
                "status": e.status_code,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "auth_config_error"},
        )

    token = login_resp.get("token") if isinstance(login_resp, dict) else None
    if not token:
        logger.error(
            "fa_login_missing_token_post_signup",
            extra={"fa_user_id": str(fa_user_id_uuid)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "auth_config_error"},
        )

    return AuthResponse(
        token=token,
        user=UserOut(
            id=user_row.id,
            email=user_row.email,
            display_name=user_row.display_name,
            role="user",
        ),
    )

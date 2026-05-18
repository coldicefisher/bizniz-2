"""GET /api/v1/auth/me route (BE-010-U1).

Auto-mounted by ``app/main.py`` under ``settings.api_v1_prefix``
(``/api/v1``), so the router below declares ``prefix='/auth'`` and the
final URL is ``/api/v1/auth/me``. Matches BE-007's ``auth_signup.py``,
BE-008's ``auth_login.py``, and BE-009's ``auth_logout.py`` prefix
convention — all four auth routes MUST agree, or the SPA hits 404 on
whichever one drifted.

Flow (simplest of the BE-007-010 quartet — middleware does the heavy
lifting):

  1. ``Depends(get_current_user)`` validates the JWT (alg=RS256 pin,
     JWKS-backed signature check, required aud/iss/exp claims) and
     produces a :class:`CurrentUser` with role pre-picked from the
     JWT roles claim per BE-006's contract. 401 cases
     (missing/invalid/expired token) and 403 (no_role_assigned)
     are raised inside the dependency and never reach this handler.
  2. SELECT the local mirror row by id. DB-unavailable / SQLAlchemy
     errors → 503 ``database_unavailable`` with a structured log.
  3. If the mirror row is missing post-middleware (narrow race window
     where ``get_current_user``'s auto-mirror upsert succeeded but
     the row vanished before our SELECT) → 500 ``user_mirror_failed``.
  4. Return :class:`UserOut` with role taken from ``current_user.role``
     (JWT-derived, authoritative), NOT ``user_row.role`` (potentially
     stale mirror snapshot). Build UserOut explicitly from the four
     named fields — do NOT ``model_dump(user_row)`` because if the
     User model grows new columns they would leak into the response.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.auth import CurrentUser, get_current_user
from app.db.session import get_db
from app.repositories.user_repository import get_user_by_id
from app.schemas.auth import ErrorResponse, UserOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get(
    "/me",
    status_code=200,
    response_model=UserOut,
    responses={
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def get_me(
    current_user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    """Return the authenticated caller's public profile.

    401 cases (missing / invalid / expired JWT) are raised by the
    ``get_current_user`` dependency and never reach this body. 403
    (no role) likewise. Reaching the handler means we have a
    validated :class:`CurrentUser` whose ``role`` was derived from
    the JWT roles claim.

    Read the local mirror to pick up the latest ``email`` and
    ``display_name`` (mirror is the authoritative source for those
    snapshot fields). The mirror's ``role`` column is ignored —
    JWT wins. Step 3 (mirror missing post-middleware) only fires in
    a narrow race window because ``get_current_user`` already
    auto-creates the row when absent; in steady state this branch
    is effectively unreachable but the spec requires the typed
    error.
    """
    try:
        user_row = await get_user_by_id(db, current_user.id)
    except OperationalError as e:
        logger.error(
            "db_unavailable_on_get_me",
            extra={
                "user_id": str(current_user.id),
                "error_type": type(e).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={"error": "database_unavailable"},
        )
    except SQLAlchemyError as e:
        logger.error(
            "db_error_on_get_me",
            extra={
                "user_id": str(current_user.id),
                "error_type": type(e).__name__,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={"error": "database_unavailable"},
        )

    if user_row is None:
        logger.error(
            "user_mirror_missing_post_middleware",
            extra={"user_id": str(current_user.id)},
        )
        raise HTTPException(
            status_code=500,
            detail={"error": "user_mirror_failed"},
        )

    return UserOut(
        id=user_row.id,
        email=user_row.email,
        display_name=user_row.display_name,
        role=current_user.role,
    )

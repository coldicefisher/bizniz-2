"""POST /api/v1/auth/logout route (BE-009-U1).

Auto-mounted by ``app/main.py`` under ``settings.api_v1_prefix``
(``/api/v1``), so the router below declares ``prefix='/auth'`` and the
final URL is ``/api/v1/auth/logout``. Matches BE-007's
``auth_signup.py`` and BE-008's ``auth_login.py`` prefix convention —
all three auth routes MUST agree, or the SPA hits 404 on whichever one
drifted.

Best-effort audit semantics
---------------------------
Logout is stateless in this milestone — no server-side session to
invalidate — and is documented in the auth contract as idempotent:
ANY input (missing token, malformed scheme, expired token, bad
signature, JWKS unavailable, FA blip, etc.) returns 204. The point of
this route is the audit hook, not enforcement.

That means we MUST NOT use ``Depends(get_current_user)`` here:
``get_current_user`` raises 401/403/503 on every failure mode the spec
demands we ignore. Instead, we attempt validation inline, and the
ENTIRE audit-log path is wrapped in ``try: ... except Exception:
pass`` so that no failure mode escapes and turns into a non-204
response. The outer ``except Exception`` deliberately catches
``HTTPException`` too (which is an Exception subclass in starlette) —
that is what "best-effort mode" means per the spec. Narrowing to
specific exception types would re-introduce the 4xx/5xx leak the spec
exists to prevent.

GET on this path returns 405 automatically (FastAPI default) because
only POST is registered.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, Response, status

from app.core import auth as core_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/logout",
    status_code=204,
    response_class=Response,
    responses={204: {"description": "Logged out"}},
)
async def logout(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Response:
    """Best-effort logout with optional JWT audit.

    Returns 204 unconditionally. If a valid Bearer JWT is presented,
    emits a structured INFO log ``logout`` with
    ``{event, user_id, ts}`` in ``extra``. Any failure during the
    audit path (missing header, malformed scheme, invalid token,
    expired token, JWKS unavailable, FA outage) is swallowed — the
    response is still 204.

    The body is empty: 204 with a JSON payload is a protocol
    violation, so we return ``Response(status_code=204)`` directly
    instead of letting FastAPI serialize a model.
    """
    if not authorization:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    try:
        if not authorization.startswith("Bearer "):
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        token = authorization[len("Bearer "):].strip()
        if not token:
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        # BE-006 exposes only the private validation helpers — reuse
        # them here to preserve the alg=RS256 pin + JWKS-cache
        # invariants. Re-implementing JWT parsing in this route would
        # silently lose those defenses (alg=none / HS256-with-public-key
        # downgrade). HTTPException raised by either helper is caught
        # by the outer ``except Exception`` below — best-effort means
        # we MUST NOT propagate auth failures from this route.
        header = core_auth._decode_unverified_header(token)
        claims = await core_auth._verify_jwt_signature_and_claims(token, header)

        sub = claims.get("sub")
        if not sub:
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        logger.info(
            "logout",
            extra={
                "event": "logout",
                "user_id": str(sub),
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        # Load-bearing bare except: the spec requires 204 for EVERY
        # failure mode in the audit path (malformed token, expired
        # token, FA unavailable, anything). Narrowing to specific
        # exception types would re-introduce the 4xx/5xx leak this
        # route exists to prevent.
        pass

    return Response(status_code=status.HTTP_204_NO_CONTENT)

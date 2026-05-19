"""GET /me — return identity of the authenticated caller."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.auth import get_current_user
from app.models.user import User
from app.schemas.me import UserMeResponse

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=UserMeResponse)
async def get_me(user: User = Depends(get_current_user)) -> UserMeResponse:
    """Return the authenticated caller's identity from validated JWT claims.

    The user object is populated by the skeleton's get_current_user
    dependency, which validates the Bearer JWT (RS256 via JWKS,
    issuer=acme.com, audience=primary app id) and upserts the local
    User row from claims. This handler MUST read identity exclusively
    from `user` — never from request body or query parameters.
    """
    raise NotImplementedError("issue BE-001")

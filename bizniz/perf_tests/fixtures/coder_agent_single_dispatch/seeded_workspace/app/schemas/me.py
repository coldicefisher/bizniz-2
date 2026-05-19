"""Pydantic schema for GET /me response."""
from __future__ import annotations

from pydantic import BaseModel, Field


class UserMeResponse(BaseModel):
    """Identity payload returned by GET /api/v1/me.

    All fields are read from the validated JWT claims attached to the
    request by the skeleton's get_current_user dependency.
    """

    id: str = Field(..., description="FusionAuth user id (sub claim).")
    email: str = Field(..., description="User email address.")
    full_name: str = Field(
        ...,
        description="Display name from JWT claims; may be an empty string when not set.",
    )
    roles: list[str] = Field(
        default_factory=list,
        description="Roles array read directly from the validated JWT.",
    )

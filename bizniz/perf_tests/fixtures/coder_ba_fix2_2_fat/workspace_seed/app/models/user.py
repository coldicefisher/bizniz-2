"""Local mirror of the FusionAuth user record.

This table exists so downstream Recipe Box tables (recipes,
favorites, etc.) can FK to a stable backend-local user id. The id
column is **NOT** a surrogate — it is exactly the FusionAuth
``sub`` claim, set by the caller (the mirror writer reads it from
the validated JWT). FusionAuth remains the source of truth for
credentials and role assignment; columns here are an informational
snapshot refreshed on signup / login / /me.

Why CITEXT instead of ``lower(email)`` + unique index: CITEXT gives
case-insensitive equality semantics through every query, join, and
FK relationship without per-call ``lower()`` plumbing. The
companion Alembic migration creates the ``citext`` extension on
the target database before this table is created.
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """SQLAlchemy 2.0 ORM model for the local users mirror table.

    Columns:
        id: PK — equals the FusionAuth ``sub`` claim (caller supplies).
        email: Case-insensitive unique email (CITEXT, max 254 chars).
        role: Snapshot of JWT roles claim. JWT remains authoritative.
        display_name: Optional friendly name (1-100 chars).
        created_at / updated_at: Server-managed timestamps.
    """

    __tablename__ = "users"

    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'admin', 'super_admin')",
            name="role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
    )
    email: Mapped[str] = mapped_column(
        CITEXT(254),
        unique=True,
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="user",
    )
    display_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

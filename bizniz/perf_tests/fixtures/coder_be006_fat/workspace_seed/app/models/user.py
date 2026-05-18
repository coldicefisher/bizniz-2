"""SQLAlchemy ORM models for the skeleton's local user mirror.

This file is owned by the skeleton — DO NOT modify it. Engineers
adding domain models should create their own file at
``app/models/<feature>.py`` (e.g. ``app/models/property.py``,
``app/models/contact.py``). The skeleton's model auto-loader
(see app/models/__init__.py) imports every ``*.py`` in this
directory at startup, so SQLAlchemy registers all your tables
without you editing any shared file.

Roles are NOT stored locally. FusionAuth owns role assignment, and
roles flow through the JWT's ``roles`` claim on every request. There
is no local Role or UserRole table — having one creates a stale
mirror that drifts from FusionAuth and breaks under async lazy-load.
If you need to query "all users with role X," ask the
FusionAuthOrchestrator (it has ``crawl()`` for that).
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    String, Text, Boolean, func,
)
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "user"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # No password_hash — FusionAuth owns credentials.
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    avatar_url: Mapped[Optional[str]] = mapped_column(Text)
    bio: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # OAuth fields removed — FusionAuth manages identity providers.
    # Roles removed — JWT claim is the only source of role truth.

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

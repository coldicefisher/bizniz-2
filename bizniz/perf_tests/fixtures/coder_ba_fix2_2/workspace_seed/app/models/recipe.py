"""SQLAlchemy 2.0 ORM model for the ``recipes`` table.

DDL for this table is owned by the BE-001 migration in
``app/db/migrations/recipes.py`` (CHECK constraints, FK with
ON DELETE CASCADE, ``gen_random_uuid()`` extension wiring). This
module is read/write mapping only — it deliberately omits any
``__table_args__`` for indexes or CHECK constraints so the
migration remains the single source of truth for schema DDL.

The ``ingredients`` and ``instructions`` columns use JSONB to
match the migration's storage choice: today each entry is a
trimmed non-empty string (enforced by the API layer), but JSONB
lets future milestones evolve list items into structured objects
without a column-type migration.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Recipe(Base):
    """SQLAlchemy 2.0 ORM model for the recipes table.

    Columns:
        id: PK — server-generated via ``gen_random_uuid()`` (pgcrypto).
        owner_id: FK to ``users.id`` (ON DELETE CASCADE in the migration).
        title: Recipe title (validated 1-200 chars at the API layer).
        description: Longer prose description (1-5000 chars at the API).
        ingredients: Ordered list of ingredient lines (JSONB).
        instructions: Ordered list of instruction steps (JSONB).
        prep_time: Prep minutes (0-1440 at the API / DB CHECK).
        cook_time: Cook minutes (0-1440 at the API / DB CHECK).
        servings: Servings count (1-1000 at the API / DB CHECK).
        created_at / updated_at: Server-managed timestamps.
    """

    __tablename__ = "recipes"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    description: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )
    ingredients: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
    )
    instructions: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
    )
    prep_time: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    cook_time: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    servings: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
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

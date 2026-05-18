"""create users table + citext extension

Revision ID: 0001_create_users
Revises:
Create Date: 2026-05-16

Mirrors the column shape of ``app.models.user.User`` (BE-002-U1):

- ``id``           : UUID, primary key (equals FusionAuth ``sub`` claim)
- ``email``        : CITEXT(254), NOT NULL, UNIQUE (case-insensitive)
- ``role``         : VARCHAR(20), NOT NULL, DEFAULT 'user', CHECK in
                     {'user', 'admin', 'super_admin'}
- ``display_name`` : VARCHAR(100), NULL
- ``created_at``   : TIMESTAMPTZ, NOT NULL, DEFAULT now()
- ``updated_at``   : TIMESTAMPTZ, NOT NULL, DEFAULT now()

The ``citext`` extension is created first (idempotently) so the
``email`` column's CITEXT type resolves. Downgrade reverses the
order: drop the table, then drop the extension.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# Alembic identifiers.
revision: str = "0001_create_users"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the ``citext`` extension and the ``users`` table."""
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "email",
            CITEXT(254),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.String(length=20),
            nullable=False,
            server_default="user",
        ),
        sa.Column(
            "display_name",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint(
            "role IN ('user', 'admin', 'super_admin')",
            name="ck_users_role",
        ),
    )


def downgrade() -> None:
    """Drop the ``users`` table and the ``citext`` extension."""
    op.drop_table("users")
    op.execute("DROP EXTENSION IF EXISTS citext")

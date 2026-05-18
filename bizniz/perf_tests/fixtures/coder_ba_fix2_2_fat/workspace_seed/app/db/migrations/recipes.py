"""Recipes table migration.

Creates the ``recipes`` table backing the Recipe entity, along with
the ``pgcrypto`` extension required for ``gen_random_uuid()`` as the
column default for ``id``.

Storage choice for ``ingredients`` / ``instructions``: **jsonb** rather
than ``text[]``. Both natively preserve list ordering on Postgres, but
jsonb lets later milestones evolve each entry from a plain string into
a structured object (e.g. ``{"name": "flour", "amount": "2 cups",
"category": "dry"}``) without another migration touching column types.
The API layer is the single source of truth for "each entry is a
non-empty trimmed string" today; the DB schema is intentionally
permissive so the type can grow.

Compound index ``ix_recipes_owner_created_id`` on
``(owner_id, created_at DESC, id DESC)`` backs the list_my_recipes
sort path: a single Postgres index scan filters by owner and yields
rows already in newest-first order with ``id DESC`` as a stable
tiebreaker for rows that share a ``created_at`` timestamp. The name
is fixed so that ``CREATE INDEX IF NOT EXISTS`` is a no-op on repeat
runs of the migration.

Idempotency: uses ``CREATE EXTENSION IF NOT EXISTS``, ``CREATE TABLE
IF NOT EXISTS``, and ``CREATE INDEX IF NOT EXISTS``. Running the
migration twice on the same database is a no-op on the second run.

Failure mode: FK creation against the ``users`` table is NOT wrapped
in try/except. If ``users`` is missing (e.g. the 0001 Alembic
migration didn't run), the CREATE TABLE call raises and the caller —
the startup migration runner — propagates the error so the container
fails to boot. A silently-missing FK would be worse: orphaned
recipes, no ON DELETE CASCADE, and a schema drift that's hard to
spot from outside the DB.
"""
from __future__ import annotations

from sqlalchemy import Connection, text


_CREATE_PGCRYPTO_EXTENSION = "CREATE EXTENSION IF NOT EXISTS pgcrypto"


_CREATE_RECIPES_TABLE = """
CREATE TABLE IF NOT EXISTS recipes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title text NOT NULL
        CHECK (length(trim(title)) BETWEEN 1 AND 200),
    description text NOT NULL
        CHECK (length(trim(description)) BETWEEN 1 AND 5000),
    ingredients jsonb NOT NULL,
    instructions jsonb NOT NULL,
    prep_time integer NOT NULL
        CHECK (prep_time BETWEEN 0 AND 1440),
    cook_time integer NOT NULL
        CHECK (cook_time BETWEEN 0 AND 1440),
    servings integer NOT NULL
        CHECK (servings BETWEEN 1 AND 1000),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""


_CREATE_RECIPES_OWNER_CREATED_ID_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_recipes_owner_created_id "
    "ON recipes (owner_id, created_at DESC, id DESC)"
)


def run_migration(connection: Connection) -> None:
    """Create the ``pgcrypto`` extension, ``recipes`` table, and sort index.

    Designed to be invoked from an async lifespan via
    ``await async_conn.run_sync(run_migration)``: SQLAlchemy bridges
    the sync ``Connection`` API to the underlying async driver.

    Idempotent — safe to call on every boot. The compound index
    ``ix_recipes_owner_created_id`` on
    ``(owner_id, created_at DESC, id DESC)`` is created with
    ``IF NOT EXISTS`` and a stable name so re-runs are no-ops.

    :param connection: A sync SQLAlchemy ``Connection`` bound to the
        target database. The caller owns the transaction; this
        function only issues DDL statements against the given
        connection and does not commit.
    """
    connection.execute(text(_CREATE_PGCRYPTO_EXTENSION))
    connection.execute(text(_CREATE_RECIPES_TABLE))
    connection.execute(text(_CREATE_RECIPES_OWNER_CREATED_ID_INDEX))

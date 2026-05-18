"""Repository functions for the ``recipes`` table.

This module owns the read/write surface against the ``recipes`` table.
Functions land here across BE-004 units U2-U6 (create, get-by-id-for-
owner, list-for-owner, update, delete); this U1 file ships the
imports-only skeleton so downstream units have a stable foundation.

Session conventions mirror :mod:`app.repositories.user_repository`:

* **Async reads** (``get_recipe_by_id_for_owner``, ``list_recipes_for_owner``)
  take an :class:`sqlalchemy.ext.asyncio.AsyncSession`, matching the
  read-side pattern of :func:`app.repositories.user_repository.get_user_by_id`.
* **Sync writes** (``create_recipe``, ``update_recipe``, ``delete_recipe``)
  take a synchronous :class:`sqlalchemy.orm.Session`, matching the
  write-side pattern of
  :func:`app.repositories.user_repository.upsert_user_mirror`.

Commit policy: repositories ``flush`` to surface DB constraint errors
to the caller, but do NOT ``commit``. The route layer owns the
transaction boundary — same convention as :mod:`user_repository` —
so a route handler can sequence the local-users self-heal upsert and
the recipe insert in a single transaction.

Exception policy: DB-unavailable / IntegrityError exceptions are
intentionally NOT swallowed here; they bubble as SQLAlchemy errors
and the route layer translates them (503 for connection issues,
400/404 for constraint violations) — same convention as
:mod:`user_repository`.

No FastAPI / HTTPException imports live in this module — HTTP-layer
translation belongs in :mod:`app.api.routes.recipes`.
"""
from typing import Optional, Sequence
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.recipe import Recipe
from app.schemas.recipe import RecipeCreate

__all__ = [
    "AsyncSession",
    "Optional",
    "Recipe",
    "RecipeCreate",
    "Sequence",
    "Session",
    "UUID",
    "create_recipe",
    "delete",
    "delete_recipe_for_owner",
    "func",
    "get_recipe_for_owner",
    "list_recipes_for_owner",
    "select",
    "update",
    "update_recipe_for_owner",
]


def create_recipe(
    session: Session,
    *,
    owner_id: UUID,
    data: RecipeCreate,
) -> Recipe:
    """Insert a new ``Recipe`` row owned by ``owner_id`` and return it.

    Constructs a :class:`app.models.recipe.Recipe` from the validated
    ``data`` payload plus the server-derived ``owner_id``, adds it to
    the session, flushes to populate the server-default columns
    (``id``, ``created_at``, ``updated_at``), and refreshes so the
    returned instance has every column readable by the caller without
    a second round-trip.

    ``owner_id`` is deliberately a keyword-only argument that does NOT
    come from ``data``: it is the authenticated user's id (the JWT
    ``sub`` claim) and any client-supplied owner_id in the request
    body must be ignored. Pydantic's ``extra='forbid'`` on
    :class:`RecipeCreate` already rejects an extra ``owner_id`` field
    in the body, but the contract is enforced here too by sourcing it
    only from this parameter.

    Commit policy: the function ``flush``es to surface IntegrityErrors
    (FK to ``users.id``, CHECK constraints) to the caller but does
    NOT ``commit``. The route layer owns the transaction boundary —
    same convention as
    :func:`app.repositories.user_repository.upsert_user_mirror` — so
    the owner-mirror self-heal upsert and the recipe insert can land
    in a single transaction.

    Exception policy: SQLAlchemy exceptions (IntegrityError,
    OperationalError, etc.) bubble unwrapped so the route layer can
    translate them (503 for connection issues, 400/404 for constraint
    violations).

    Args:
        session: Active synchronous SQLAlchemy ``Session`` bound to the
            application DB.
        owner_id: FusionAuth ``sub`` of the authenticated user; the FK
            target on ``users.id``. Server-derived — never read from
            ``data``.
        data: Validated :class:`RecipeCreate` payload. ``model_dump()``
            expands into the Recipe constructor kwargs.

    Returns:
        The freshly persisted :class:`Recipe` instance with ``id``,
        ``created_at``, and ``updated_at`` populated from server
        defaults.
    """
    recipe = Recipe(owner_id=owner_id, **data.model_dump())
    session.add(recipe)
    session.flush()
    session.refresh(recipe)
    return recipe


async def list_recipes_for_owner(
    session: AsyncSession,
    *,
    owner_id: UUID,
) -> list[Recipe]:
    """Return all recipes owned by ``owner_id`` in newest-first order.

    Issues a ``SELECT * FROM recipes WHERE owner_id = :owner_id
    ORDER BY created_at DESC, id DESC``. The DESC/DESC ordering is
    deliberately chosen to match the compound index BE-001 created on
    ``(owner_id, created_at DESC, id DESC)`` so the planner can serve
    the list via an index-only scan without a sort step. The ``id``
    tiebreaker yields a deterministic order when two rows share a
    ``created_at`` (rapid-double-submit scenario, or coarse clock
    resolution).

    Owner scoping is enforced inside the query — there is no separate
    "show me everyone's recipes" path in this milestone. Admin
    moderation ships later (see ``list_my_recipes`` capability:
    "admin role does NOT get all recipes").

    Returns an empty list when the owner has no recipes (never raises
    on absence). Always returns a concrete ``list`` (not a generator /
    ``ScalarResult``) so callers can len() / index / iterate multiple
    times without re-executing the query.

    Exception policy: SQLAlchemy exceptions (OperationalError, etc.)
    bubble unwrapped so the route layer can translate connection
    issues to ``503``. Same convention as
    :func:`app.repositories.user_repository.get_user_by_id`.

    Args:
        session: Active SQLAlchemy ``AsyncSession`` bound to the app
            DB. Read-only — this function does not write.
        owner_id: FusionAuth ``sub`` of the authenticated user; the
            FK target on ``users.id``. Server-derived from the JWT.

    Returns:
        List of :class:`Recipe` rows owned by ``owner_id``, ordered by
        ``created_at DESC, id DESC``. Empty list if the owner has no
        recipes.
    """
    stmt = (
        select(Recipe)
        .where(Recipe.owner_id == owner_id)
        .order_by(Recipe.created_at.desc(), Recipe.id.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_recipe_for_owner(
    session: AsyncSession,
    *,
    recipe_id: UUID,
    owner_id: UUID,
) -> Optional[Recipe]:
    """Fetch one recipe by id, scoped to ``owner_id``.

    Issues a ``SELECT * FROM recipes WHERE id = :recipe_id AND
    owner_id = :owner_id LIMIT 1``. The combined WHERE clause is the
    whole point of the function: it collapses the "absent row" and
    "wrong owner" cases into a single ``None`` return, which the route
    layer translates to ``404 recipe_not_found``. Distinguishing the
    two would leak existence — a user who guesses another owner's
    recipe id would learn it exists from a ``403`` vs ``404``
    discriminator.

    Owner scoping is enforced inside the query — there is no separate
    "fetch any recipe by id" path in this milestone. Admin moderation
    (cross-user read) ships later (see ``get_recipe`` capability:
    "Admin role does NOT get cross-user read in this milestone").

    ``LIMIT 1`` is redundant on a primary-key lookup (the planner will
    already stop after one match) but is added for defense in depth
    in case the PK invariant is ever weakened by a future migration.

    Exception policy: SQLAlchemy exceptions (OperationalError, etc.)
    bubble unwrapped so the route layer can translate connection
    issues to ``503``. Same convention as
    :func:`app.repositories.user_repository.get_user_by_id` and
    :func:`list_recipes_for_owner`.

    Args:
        session: Active SQLAlchemy ``AsyncSession`` bound to the app
            DB. Read-only — this function does not write.
        recipe_id: Primary key of the recipes row to fetch.
        owner_id: FusionAuth ``sub`` of the authenticated user; the
            FK target on ``users.id``. Server-derived from the JWT.

    Returns:
        The matching :class:`Recipe` row, or ``None`` when either no
        row has that ``id`` OR the row exists but is owned by a
        different user.
    """
    stmt = (
        select(Recipe)
        .where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def delete_recipe_for_owner(
    session: Session,
    *,
    recipe_id: UUID,
    owner_id: UUID,
) -> bool:
    """Delete one recipe by id, scoped to ``owner_id``; return whether it existed.

    Issues a ``DELETE FROM recipes WHERE id = :recipe_id AND
    owner_id = :owner_id``. The combined WHERE clause is the whole
    point of the function: it collapses the "absent row" and "wrong
    owner" cases into a single ``False`` return, which the route layer
    translates to ``404 recipe_not_found``. Distinguishing the two
    would leak existence — a user who guesses another owner's recipe
    id would learn it exists from a ``403`` vs ``404`` discriminator.

    The return value is ``result.rowcount > 0``: ``True`` when the
    DELETE removed the row, ``False`` when no row matched either
    because ``recipe_id`` does not exist OR the row exists but is
    owned by a different user.

    Hard delete — there is no soft-delete column in this milestone.
    No cascading writes needed at the application layer; the
    ``recipes`` table is referenced only by FKs from later-milestone
    tables (favorites, recipe_tags) whose ON DELETE CASCADE handles
    cleanup at the DB layer.

    Owner scoping is enforced inside the query — there is no separate
    "delete any recipe by id" path in this milestone. Admin
    moderation ships later (see ``delete_recipe`` capability:
    "Hard delete — no soft-delete column this milestone").

    Commit policy: the function ``flush``es to surface DB errors
    (OperationalError on a dead connection, etc.) to the caller but
    does NOT ``commit``. The route layer owns the transaction
    boundary — same convention as :func:`create_recipe` and
    :func:`app.repositories.user_repository.upsert_user_mirror` — so
    a route handler can sequence the owner-mirror self-heal and the
    delete in a single transaction.

    Exception policy: SQLAlchemy exceptions (OperationalError, etc.)
    bubble unwrapped so the route layer can translate connection
    issues to ``503``. Same convention as :func:`create_recipe`.

    Args:
        session: Active synchronous SQLAlchemy ``Session`` bound to
            the application DB.
        recipe_id: Primary key of the recipes row to delete.
        owner_id: FusionAuth ``sub`` of the authenticated user; the
            FK target on ``users.id``. Server-derived from the JWT.

    Returns:
        ``True`` if a row was deleted; ``False`` if no row matched
        (either no such id, or the row is owned by someone else).
    """
    stmt = delete(Recipe).where(
        Recipe.id == recipe_id,
        Recipe.owner_id == owner_id,
    )
    result = session.execute(stmt)
    session.flush()
    return result.rowcount > 0


def update_recipe_for_owner(
    session: Session,
    *,
    recipe_id: UUID,
    owner_id: UUID,
    data: RecipeCreate,
) -> Optional[Recipe]:
    """Full-replace a recipe owned by ``owner_id``; return updated row or None.

    Issues a Postgres ``UPDATE recipes SET <editable fields>,
    updated_at = now() WHERE id = :recipe_id AND owner_id = :owner_id
    RETURNING *``. The combined WHERE clause is the existence-leak
    guard: it collapses the "absent row" and "wrong owner" cases into a
    single ``None`` return, which the route layer translates to
    ``404 recipe_not_found``. Distinguishing the two would leak
    existence — a user who guesses another owner's recipe id would
    learn it exists from a ``403`` vs ``404`` discriminator.

    Fields written: every key returned by ``data.model_dump()``
    (title, description, ingredients, instructions, prep_time,
    cook_time, servings) plus an explicit
    ``updated_at = now()``. ``data`` is a :class:`RecipeCreate`, whose
    ``extra='forbid'`` config has already rejected any
    client-supplied ``id`` / ``owner_id`` / ``created_at`` /
    ``updated_at`` in the body — so ``model_dump()`` only yields the
    editable surface and the immutable columns are never named in the
    SET clause. Setting ``updated_at`` explicitly is belt-and-braces
    on top of the model's ``onupdate=func.now()`` mapping: the
    contract guarantees ``updated_at`` advances on every successful
    update, regardless of dialect or ORM-event configuration.

    RETURNING is Postgres-specific and lets the single statement do
    both the write and the read in one round-trip; the returned
    :class:`Recipe` instance carries the freshly-applied values plus
    the server-side ``updated_at`` timestamp without a follow-up
    SELECT. ``synchronize_session='fetch'`` keeps any
    already-loaded Recipe in the session's identity map in sync with
    the new row state.

    Owner scoping is enforced inside the query — there is no separate
    "update any recipe by id" path in this milestone. Admin
    moderation (cross-user write) ships later (see ``update_recipe``
    capability: "owner_id, id, created_at MUST NOT be mutable").

    Commit policy: the function ``flush``es to surface IntegrityError
    / OperationalError to the caller but does NOT ``commit``. The
    route layer owns the transaction boundary — same convention as
    :func:`create_recipe`, :func:`delete_recipe_for_owner`, and
    :func:`app.repositories.user_repository.upsert_user_mirror`.

    Exception policy: SQLAlchemy exceptions (IntegrityError,
    OperationalError, etc.) bubble unwrapped so the route layer can
    translate them (503 for connection issues, 400/404 for constraint
    violations). Same convention as the other repository functions.

    Args:
        session: Active synchronous SQLAlchemy ``Session`` bound to
            the application DB.
        recipe_id: Primary key of the recipes row to update.
        owner_id: FusionAuth ``sub`` of the authenticated user; the
            FK target on ``users.id``. Server-derived from the JWT.
        data: Validated :class:`RecipeCreate` payload with the
            replacement field values.

    Returns:
        The updated :class:`Recipe` row, or ``None`` when no row
        matched (either ``recipe_id`` does not exist OR the row
        exists but is owned by a different user).
    """
    stmt = (
        update(Recipe)
        .where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
        .values(**data.model_dump(), updated_at=func.now())
        .returning(Recipe)
        .execution_options(synchronize_session="fetch")
    )
    result = session.execute(stmt)
    updated = result.scalar_one_or_none()
    session.flush()
    return updated

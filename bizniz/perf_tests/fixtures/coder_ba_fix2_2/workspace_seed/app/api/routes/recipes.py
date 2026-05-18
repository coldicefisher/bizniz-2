"""Recipes router (BE-006).

Auto-mounted by ``app/main.py`` under ``settings.api_v1_prefix``
(``/api/v1``), so the router below declares ``prefix='/recipes'`` and
the final URLs are ``/api/v1/recipes/...``. Matches the convention in
``app/api/routes/auth_login.py`` (declare only the feature prefix and
let the auto-mount add ``/api/v1``). Declaring ``/api/recipes`` here
would double-prefix to ``/api/v1/api/recipes/...``.

This module hosts the BE-006 CRUD handlers. The POST handler (this
unit, BE-006-U2) creates a recipe owned by the authenticated caller;
the GET-list / GET-by-id / PUT / DELETE handlers land in U3-U6 and
the 422→400 UUID-coercion exception handler in U7.

Auth + ownership contract
-------------------------
Every handler gates on ``Depends(require_roles(['user', 'admin']))``
so a missing/invalid JWT short-circuits to 401 (from
``get_current_user``) and a token without the user/admin role
short-circuits to 403 BEFORE the handler body runs. owner_id is
derived ONLY from the authenticated identity (via
``ensure_local_user``) — any client-supplied owner_id in the request
body is rejected by ``RecipeCreate``'s ``extra='forbid'`` config and
never reaches this layer.

Async/sync session bridge
-------------------------
``get_db`` yields an :class:`AsyncSession`. The repository's
``create_recipe`` takes a synchronous :class:`Session` (mirrors the
write-side pattern of :func:`upsert_user_mirror`). Bridge with
``await db.run_sync(lambda s: create_recipe(s, ...))`` — same pattern
the BA-fix1-1 repair adopted in ``auth_login.py`` and
``auth_signup.py``. ``get_db`` auto-commits the request transaction
when the handler returns successfully, so this module does not call
``db.commit()`` explicitly — the entire flow (mirror self-heal +
recipe insert) lands in a single transaction.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.auth import CurrentUser, require_roles
from app.db.session import get_db
from app.models.recipe import Recipe
from app.repositories.recipes import (
    create_recipe,
    delete_recipe_for_owner,
    get_recipe_for_owner,
    list_recipes_for_owner,
)
from app.schemas.recipe import RecipeCreate, RecipeOut
from app.services.owner_mirror import ensure_local_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recipes", tags=["recipes"])


def _emit_audit(
    *,
    event: str,
    user_id: UUID,
    recipe_id: UUID,
    request: Optional[Request] = None,
) -> None:
    """Emit a structured audit-log entry for a recipe mutation (BE-006-fix1).

    Per the cross-cutting logging spec, every successful recipe
    create/update/delete MUST emit a structured log entry shaped:

    ``{event, user_id, recipe_id, request_id, ts}``

    where ``event`` is one of ``recipe_created`` / ``recipe_updated`` /
    ``recipe_deleted``, ``ts`` is an ISO-8601 UTC timestamp, and
    ``request_id`` is sourced from ``request.state.request_id`` when
    the request-id middleware has populated it (None otherwise — the
    middleware is a downstream concern).

    Encoded as a single JSON document so log aggregators can index
    every field; emitted at INFO level so it's captured by default in
    production log shipping. The route layer calls this AFTER the
    repository commits the mutation — failed writes do NOT emit an
    audit event.
    """
    payload = {
        "event": event,
        "user_id": str(user_id),
        "recipe_id": str(recipe_id),
        "request_id": (
            getattr(request.state, "request_id", None)
            if request is not None
            else None
        ),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(json.dumps(payload))


def _validate_recipe_id(recipe_id: str = Path(...)) -> UUID:
    """Parse the ``recipe_id`` path segment into a :class:`UUID`, 400 on failure.

    BE-006-U7: spec requires 400, not 422, on malformed UUID. FastAPI's
    default behavior — declaring ``recipe_id: UUID`` directly on the
    handler — surfaces a malformed segment as 422 via the
    ``RequestValidationError`` handler, which violates the
    ``get_recipe`` / ``update_recipe`` / ``delete_recipe`` capability
    contracts (each explicitly lists ``400 — recipe_id is not a valid
    UUID`` / ``400 — malformed UUID``).

    The dependency-based coercion below is preferred over a router- or
    app-scoped ``RequestValidationError`` handler because it scopes the
    400 narrowly to malformed UUID path params only. Body validation
    failures (extra/unknown fields, type mismatches in ``RecipeCreate``)
    are handled separately — they don't share this code path.

    No global handler is registered in ``app/main.py`` or
    ``app/core/`` (verified at U7 implementation time), so this is the
    sole 422→400 coercion in the recipes router.
    """
    try:
        return UUID(recipe_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_recipe_id",
        )


def _claims_from_current_user(user: CurrentUser) -> dict:
    """Reconstruct the minimal JWT-claims dict ``ensure_local_user`` expects.

    ``get_current_user`` already validated the JWT and projected its
    interesting fields onto :class:`CurrentUser` (``id``, ``email``,
    ``display_name``, ``role``); the raw claims dict is not kept. The
    self-heal helper, however, accepts a ``jwt_claims`` mapping shaped
    like the original JWT payload (``sub`` / ``email`` / ``name``).

    Rebuild that minimum-viable view here:

    * ``sub`` ← ``str(user.id)`` — the helper re-parses to UUID.
    * ``email`` ← ``user.email`` — required for mirror upsert.
    * ``name`` ← ``user.display_name`` — optional; the helper reads
      ``name`` or ``preferred_username``, either of which may be absent.

    Keeping the bridge in one named helper means future handlers that
    call ``ensure_local_user`` re-use the same shape instead of
    open-coding it inconsistently.
    """
    return {
        "sub": str(user.id),
        "email": user.email,
        "name": user.display_name,
    }


@router.post(
    "",
    response_model=RecipeOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_recipe_endpoint(
    payload: RecipeCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(["user", "admin"])),
) -> RecipeOut:
    """Create a recipe owned by the authenticated caller.

    Pipeline:

    1. ``require_roles(['user', 'admin'])`` runs first — composes
       :func:`get_current_user`, which validates the FA-issued JWT
       (alg=RS256 pin, JWKS-backed signature, required aud/iss/exp
       claims) and raises 401 on missing/invalid tokens. The composed
       role check then raises 403 if the JWT carries no user/admin
       role. Either way, those error responses never reach this body.

    2. ``RecipeCreate`` ran during request parsing — strict-mode
       Pydantic v2 with ``extra='forbid'`` (rejects unknown fields),
       ``str_strip_whitespace=True`` (trims before length checks), and
       integer fields that reject floats and strings. Per-item
       validators on ``ingredients`` / ``instructions`` reject empty
       trimmed entries and oversize lines. Failures surface as 422
       via FastAPI's default RequestValidationError handler;
       BE-006-U7 collapses the 422 envelope to 400 for the capability
       contract.

    3. ``ensure_local_user`` performs the mirror self-heal — if the
       authenticated user's local ``users`` row is missing (legacy /
       seeded FA user that never went through signup), the helper
       upserts it from the JWT claims and returns the parsed
       ``owner_id``. owner_id is sourced ONLY from the JWT here —
       any client-supplied owner_id is already blocked by
       ``extra='forbid'``, and this code never reads from ``payload``
       for the owner.

    4. ``create_recipe`` (sync repository function) inserts the row.
       Bridge the AsyncSession onto the sync session via
       ``db.run_sync`` — same pattern as the BA-fix1-1 mirror upserts
       in ``auth_login`` and ``auth_signup``. The repository flushes
       and refreshes so the returned :class:`Recipe` carries the
       server-defaulted ``id``, ``created_at``, and ``updated_at``.

    5. Return the freshly persisted row; FastAPI's
       ``response_model=RecipeOut`` projects it through the
       ``from_attributes=True`` schema. ``get_db`` commits the
       request transaction when the handler returns — the mirror
       self-heal and the recipe insert land in a single transaction.
    """
    claims = _claims_from_current_user(user)
    owner_id = await ensure_local_user(db, jwt_claims=claims)
    recipe = await db.run_sync(
        lambda s: create_recipe(s, owner_id=owner_id, data=payload)
    )
    _emit_audit(
        event="recipe_created",
        user_id=user.id,
        recipe_id=recipe.id,
        request=request,
    )
    return recipe


@router.get(
    "/mine",
    response_model=list[RecipeOut],
    status_code=status.HTTP_200_OK,
)
async def list_my_recipes_endpoint(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(["user", "admin"])),
) -> list[RecipeOut]:
    """Return the authenticated caller's recipes, newest first.

    Pipeline:

    1. ``require_roles(['user', 'admin'])`` runs first — composes
       :func:`get_current_user`, which validates the FA-issued JWT
       and raises 401 on missing/invalid tokens. The composed role
       check raises 403 if the JWT carries no user/admin role.
       Either short-circuit response is returned before this body
       runs.

    2. ``list_recipes_for_owner`` issues
       ``SELECT * FROM recipes WHERE owner_id = :owner_id
       ORDER BY created_at DESC, id DESC`` against the
       :class:`AsyncSession`. Owner scoping happens inside the query
       so admins do NOT see other users' recipes (admin moderation
       ships in a later milestone). The repository returns a concrete
       ``list`` — empty when the caller has no recipes — never raises
       on absence.

    3. No mirror self-heal here — the list endpoint is read-only and
       has no FK dependency on ``users``. If the caller's row is
       missing the SELECT simply returns ``[]`` (no row would have
       ``owner_id`` matching a non-existent user anyway). The POST
       handler is where the self-heal upsert lives.

    Query params are intentionally undeclared — FastAPI's default
    behaviour is to ignore unknown query strings (no 4xx), which
    matches the ``list_my_recipes`` capability contract's
    "ignore any query parameters (pagination ships later); do not
    reject, just ignore."

    Path ordering note: this handler is declared BEFORE the
    forthcoming ``GET /{recipe_id}`` (BE-006-U4) on purpose.
    FastAPI matches routes in registration order, so a literal
    ``/mine`` declared after a parameterised ``/{recipe_id}`` would
    be shadowed (``/mine`` would parse as ``recipe_id='mine'`` and
    400 on the UUID coercion).
    """
    recipes = await list_recipes_for_owner(db, owner_id=user.id)
    return recipes


@router.get(
    "/{recipe_id}",
    response_model=RecipeOut,
    status_code=status.HTTP_200_OK,
)
async def get_recipe_endpoint(
    recipe_id: UUID = Depends(_validate_recipe_id),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(["user", "admin"])),
) -> RecipeOut:
    """Return a single recipe by id, scoped to the authenticated caller.

    Pipeline:

    1. ``require_roles(['user', 'admin'])`` runs first — composes
       :func:`get_current_user`, which validates the FA-issued JWT
       (RS256, JWKS-backed, required ``aud``/``iss``/``exp``) and
       short-circuits with 401 on missing/invalid tokens. The role
       gate then returns 403 if the JWT carries no user/admin role.
       Either response is returned before this body runs.

    2. ``recipe_id`` is parsed by FastAPI as a :class:`UUID` — a
       malformed UUID in the path surfaces as 422 from the default
       request-validation handler; BE-006-U7 collapses that to 400
       for the public capability contract.

    3. No mirror self-heal — the contract notes that
       ``ensure_local_user`` is NOT required here. The recipes-table
       FK to ``users.id`` is enforced at INSERT time only; an existing
       recipe row implies its ``owner_id`` already exists in
       ``users``, and a missing row collapses to 404 either way.
       Keeping the mirror-upsert off the read path avoids needless
       write traffic on the 99% case of a 404 wrong-owner request.

    4. ``get_recipe_for_owner`` issues a single
       ``SELECT * FROM recipes WHERE id = :recipe_id AND
       owner_id = :owner_id LIMIT 1`` against the
       :class:`AsyncSession`. The combined WHERE clause collapses
       "absent row" and "wrong owner" into a single ``None`` return,
       which translates to ``404 recipe_not_found`` here. Distinguishing
       the two would leak existence — a user who guesses another
       owner's recipe id would learn it exists from a ``403`` vs
       ``404`` discriminator.

    5. Admin role does NOT get cross-user read in this milestone
       (admin moderation ships later); ``owner_id`` is always
       sourced from the JWT identity via ``user.id``.

    6. ``response_model=RecipeOut`` projects the returned ORM row
       through the ``from_attributes=True`` schema so the caller
       receives the full recipe representation (id, owner_id,
       title, description, ingredients, instructions, prep_time,
       cook_time, servings, created_at, updated_at).

    Path ordering note: this handler is declared AFTER
    ``GET /mine`` on purpose. FastAPI matches routes in registration
    order, so a literal ``/mine`` declared after a parameterised
    ``/{recipe_id}`` would be shadowed (``/mine`` would parse as
    ``recipe_id='mine'`` and 422 on the UUID coercion).
    """
    recipe = await get_recipe_for_owner(
        db, recipe_id=recipe_id, owner_id=user.id
    )
    if recipe is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="recipe_not_found",
        )
    return recipe


def _update_recipe_for_owner(
    session: Session,
    *,
    recipe_id: UUID,
    owner_id: UUID,
    data: RecipeCreate,
) -> Optional[Recipe]:
    """Sync helper: full-replace a recipe owned by ``owner_id``; return it or None.

    Mirrors the read+update pattern of the eventual BE-004-U5
    repository function (which has not shipped at the time this
    handler lands). Defined here as a private helper so the PUT
    endpoint can express its contract in one place without depending
    on a not-yet-merged repo symbol; once BE-004-U5 ships, this
    helper can be deleted and the handler swapped to import
    ``update_recipe_for_owner`` from the repository module without
    changing the route's externally observable behavior.

    Behaviour:

    * Issues ``SELECT * FROM recipes WHERE id = :recipe_id AND
      owner_id = :owner_id LIMIT 1``. The combined WHERE clause
      collapses the "absent row" and "wrong owner" cases into a
      single ``None`` return — the route layer translates that to
      ``404 recipe_not_found``. Distinguishing the two would leak
      existence (a user who guesses another owner's recipe id would
      learn it exists from a ``403`` vs ``404`` discriminator).

    * On match, applies every field of the validated
      :class:`RecipeCreate` payload to the loaded ORM instance.
      ``RecipeCreate``'s ``extra='forbid'`` already blocks any
      attempt to smuggle ``id`` / ``owner_id`` / ``created_at`` /
      ``updated_at`` in the body, so ``data.model_dump()`` only
      yields the editable fields (title, description, ingredients,
      instructions, prep_time, cook_time, servings). The server-
      managed ``updated_at`` column is refreshed by the ORM's
      ``onupdate=func.now()`` when the flush emits the UPDATE.

    * ``flush`` surfaces IntegrityError / OperationalError to the
      caller; ``refresh`` repopulates the server-defaulted columns
      (notably ``updated_at``) so the returned instance carries the
      post-update wall-clock timestamp without a second round-trip.

    * No commit — the route layer (via ``get_db``) owns the
      transaction boundary, same as :func:`create_recipe`.

    Args:
        session: Active synchronous SQLAlchemy ``Session`` bridged
            from the request's :class:`AsyncSession` via
            ``db.run_sync``.
        recipe_id: Primary key of the recipes row to update.
        owner_id: FusionAuth ``sub`` of the authenticated user.
        data: Validated :class:`RecipeCreate` payload with the
            replacement field values.

    Returns:
        The updated :class:`Recipe` row, or ``None`` when no row
        matched (either ``recipe_id`` does not exist OR the row
        exists but is owned by a different user).
    """
    stmt = (
        select(Recipe)
        .where(Recipe.id == recipe_id, Recipe.owner_id == owner_id)
        .limit(1)
    )
    recipe = session.execute(stmt).scalar_one_or_none()
    if recipe is None:
        return None
    for field, value in data.model_dump().items():
        setattr(recipe, field, value)
    session.flush()
    session.refresh(recipe)
    return recipe


@router.put(
    "/{recipe_id}",
    response_model=RecipeOut,
    status_code=status.HTTP_200_OK,
)
async def update_recipe_endpoint(
    payload: RecipeCreate,
    request: Request,
    recipe_id: UUID = Depends(_validate_recipe_id),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(["user", "admin"])),
) -> RecipeOut:
    """Full-replace one of the authenticated caller's recipes.

    Pipeline:

    1. ``require_roles(['user', 'admin'])`` runs first — composes
       :func:`get_current_user`, which validates the FA-issued JWT
       (RS256, JWKS-backed, required ``aud``/``iss``/``exp``) and
       short-circuits with 401 on missing/invalid tokens. The role
       gate then returns 403 if the JWT carries no user/admin
       role. Either response is returned before this body runs.

    2. ``recipe_id`` is parsed by FastAPI as a :class:`UUID` — a
       malformed UUID in the path surfaces as 422 from the default
       request-validation handler; BE-006-U7 collapses that to 400
       for the public capability contract.

    3. ``RecipeCreate`` ran during request parsing — strict-mode
       Pydantic v2 with ``extra='forbid'`` (rejects unknown fields
       including any client-supplied ``owner_id`` / ``id`` /
       ``created_at`` / ``updated_at``), ``str_strip_whitespace=
       True`` (trims before length checks), and integer fields that
       reject floats and strings. Failures surface as 422 via the
       default RequestValidationError handler.

    4. No mirror self-heal — the contract explicitly notes that
       ensure_local_user is NOT required here. The recipes-table
       FK to ``users.id`` is enforced at INSERT time only; an
       existing recipe row implies its ``owner_id`` already exists
       in ``users``, and the UPDATE re-uses the same owner_id
       sourced from the JWT, so the FK invariant cannot regress.
       Keeping the mirror-upsert off the write path here avoids
       needless writes when the row is going to fail the ownership
       check anyway (the 99% case of a 404 wrong-owner request).

    5. The actual update runs inside ``db.run_sync(lambda s: _update_recipe_for_owner(s, ...))`` —
       same async→sync bridge pattern as :func:`create_recipe_endpoint`.
       The helper performs the SELECT+mutation+flush+refresh in a
       single sync session call.

    6. ``None`` from the helper → 404 ``recipe_not_found`` — the
       combined ``id = :recipe_id AND owner_id = :owner_id``
       WHERE clause collapses "absent row" and "wrong owner" into
       a single response, preventing the 403-vs-404 existence leak
       (a user must not be able to learn another owner's recipe id
       exists by comparing error codes).

    7. ``response_model=RecipeOut`` projects the returned ORM row
       through the ``from_attributes=True`` schema, carrying the
       refreshed ``updated_at`` (later than ``created_at`` thanks
       to the model's ``onupdate=func.now()`` mapping).
    """
    recipe = await db.run_sync(
        lambda s: _update_recipe_for_owner(
            s, recipe_id=recipe_id, owner_id=user.id, data=payload
        )
    )
    if recipe is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="recipe_not_found",
        )
    _emit_audit(
        event="recipe_updated",
        user_id=user.id,
        recipe_id=recipe.id,
        request=request,
    )
    return recipe


@router.delete(
    "/{recipe_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_recipe_endpoint(
    request: Request,
    recipe_id: UUID = Depends(_validate_recipe_id),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(["user", "admin"])),
) -> None:
    """Hard-delete one of the authenticated caller's recipes.

    Pipeline:

    1. ``require_roles(['user', 'admin'])`` runs first — composes
       :func:`get_current_user`, which validates the FA-issued JWT
       (RS256, JWKS-backed, required ``aud``/``iss``/``exp``) and
       short-circuits with 401 on missing/invalid tokens. The role
       gate then returns 403 if the JWT carries no user/admin role.
       Either response is returned before this body runs.

    2. ``recipe_id`` is parsed by FastAPI as a :class:`UUID` — a
       malformed UUID in the path surfaces as 422 from the default
       request-validation handler; BE-006-U7 collapses that to 400
       for the public capability contract.

    3. No mirror self-heal — the contract notes that
       ``ensure_local_user`` is NOT required here. The recipes-table
       FK to ``users.id`` is enforced at INSERT time only; an
       existing recipe row implies its ``owner_id`` already exists
       in ``users``. Skipping the mirror-upsert keeps the
       wrong-owner 404 path off the write side entirely.

    4. The repository's :func:`delete_recipe_for_owner` is a SYNC
       function — bridge via ``await db.run_sync(...)``, same
       pattern as :func:`create_recipe_endpoint` and
       :func:`update_recipe_endpoint`. The single DELETE statement
       has a combined ``id = :recipe_id AND owner_id = :owner_id``
       WHERE clause, so absent-row and wrong-owner collapse to the
       same ``False`` return (no existence leak via 403 vs 404).

    5. ``False`` from the repo → 404 ``recipe_not_found``. This is
       the idempotency boundary: a second DELETE on the same id
       also returns 404 because the row is genuinely gone — clients
       should treat both 204 and 404 after a delete as "gone".

    6. ``True`` → returning ``None`` with the declared
       ``status_code=204`` produces an empty response body
       (FastAPI default for ``None`` returns).
    """
    deleted = await db.run_sync(
        lambda s: delete_recipe_for_owner(
            s, recipe_id=recipe_id, owner_id=user.id
        )
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="recipe_not_found",
        )
    _emit_audit(
        event="recipe_deleted",
        user_id=user.id,
        recipe_id=recipe_id,
        request=request,
    )
    return None

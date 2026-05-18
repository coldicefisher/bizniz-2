"""Domain helpers — already shipped by BE-005 (seeded for perf test)."""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.schemas.recipe import RecipeCreate
    from app.models.recipe import Recipe


async def create_recipe(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    data: "RecipeCreate",
) -> "Recipe":
    """Insert a recipe row owned by ``owner_id``. Returns the
    refreshed Recipe ORM object."""
    from app.models.recipe import Recipe
    recipe = Recipe(
        owner_id=owner_id,
        title=data.title,
        description=data.description,
        ingredients=data.ingredients,
        instructions=data.instructions,
        prep_time_minutes=data.prep_time_minutes,
        cook_time_minutes=data.cook_time_minutes,
        servings=data.servings,
    )
    session.add(recipe)
    await session.flush()
    await session.refresh(recipe)
    return recipe


async def ensure_local_user(
    session: AsyncSession, *, jwt_claims: dict,
) -> uuid.UUID:
    """Mirror-upsert the User row keyed on the JWT ``sub`` and return
    the local user_id. Already shipped by BE-002."""
    from app.models.user import User
    from sqlalchemy import select
    sub = jwt_claims.get("sub")
    if not sub:
        raise ValueError("JWT missing 'sub' claim")
    user_id = uuid.UUID(sub)
    result = await session.execute(
        select(User).where(User.user_id == user_id),
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = User(
            user_id=user_id,
            email=jwt_claims.get("email") or "",
            first_name=jwt_claims.get("given_name") or "",
            last_name=jwt_claims.get("family_name") or "",
            email_verified=bool(jwt_claims.get("email_verified")),
        )
        session.add(row)
        await session.flush()
    return user_id

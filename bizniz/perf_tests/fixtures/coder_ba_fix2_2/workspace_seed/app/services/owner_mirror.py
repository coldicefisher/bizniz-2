"""Self-heal helper for the local ``users`` mirror.

Used by route handlers that need a local ``users`` row to exist before
they can insert child rows (recipes, favorites, etc.) keyed on
``owner_id``. The mirror is normally created on signup and refreshed on
login / ``/me``, but a JWT issued before the row was mirrored — legacy
or FA-only seeded users, or a row that vanished — can still arrive at a
downstream route. :func:`ensure_local_user` closes that race idempotently.

This matches the milestone-1 self-heal pattern already in
:mod:`app.api.routes.auth_login` and :mod:`app.core.auth`: async lookup
via :func:`app.repositories.user_repository.get_user_by_id`, then the
synchronous :func:`app.repositories.user_repository.upsert_user_mirror`
bridged onto the AsyncSession with ``await session.run_sync(...)``.

The helper does NOT commit. The surrounding request transaction owns
commit / rollback — same contract as ``upsert_user_mirror`` itself.
"""
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.user_repository import (
    get_user_by_id,
    upsert_user_mirror,
)


async def ensure_local_user(
    session: AsyncSession, *, jwt_claims: dict
) -> UUID:
    """Ensure the local users mirror row exists for ``jwt_claims['sub']``.

    Fast path: if the row already exists, returns the parsed UUID
    immediately without writing.

    Self-heal path: if missing, performs an idempotent
    ``INSERT ... ON CONFLICT (id) DO NOTHING ... RETURNING`` via the
    milestone-1 :func:`upsert_user_mirror` (synchronous; bridged here
    via ``await session.run_sync(...)``). The mirror row is populated
    from claims:

    * ``id`` ← ``UUID(claims['sub'])``
    * ``email`` ← ``claims['email']`` (lowercased inside
      ``upsert_user_mirror``)
    * ``role`` ← always ``'user'`` — the JWT roles claim is
      authoritative for authz; the mirror ``role`` column is an
      informational snapshot only.
    * ``display_name`` ← ``claims['name']`` or
      ``claims['preferred_username']`` (either may be absent)

    Does NOT commit — the caller's request transaction owns commit /
    rollback. :class:`~app.repositories.user_repository.DuplicateEmailInMirror`
    and other SQLAlchemy errors propagate untouched; the route layer
    translates them.

    Args:
        session: Active SQLAlchemy ``AsyncSession`` bound to the
            application DB.
        jwt_claims: Validated JWT claims dict. MUST contain ``sub``
            (UUID-parseable) and ``email``.

    Returns:
        ``owner_id`` — the FA ``sub`` claim parsed as a :class:`uuid.UUID`.
    """
    owner_id = UUID(str(jwt_claims["sub"]))

    existing = await get_user_by_id(session, owner_id)
    if existing is not None:
        return owner_id

    email = jwt_claims["email"]
    display_name = (
        jwt_claims.get("name") or jwt_claims.get("preferred_username")
    )
    await session.run_sync(
        lambda s: upsert_user_mirror(
            s,
            fa_user_id=owner_id,
            email=email,
            role="user",
            display_name=display_name,
        )
    )
    return owner_id

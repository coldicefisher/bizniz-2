"""Repository functions for the local users mirror table.

This module owns the read/write surface against the ``users`` table,
which mirrors FusionAuth identities into Postgres so application
tables (recipes, favorites, tags) can FK to a stable local row.

Exception contract:

* :class:`DuplicateEmailInMirror` — raised by :func:`upsert_user_mirror`
  when the email-unique index fires for a row whose primary key (the
  FusionAuth ``sub`` claim) does not match the row already mapped to
  that email. This indicates two distinct FA user IDs sharing the same
  email — pathological in steady state, but the auth router needs a
  typed signal to translate into ``500 duplicate_email_in_mirror``.

DB-unavailable failures (connection refused, deadlock, etc.) are
intentionally NOT wrapped here; they bubble naturally as SQLAlchemy
exceptions and the route layer translates them to ``503``.
"""
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.user import User


class DuplicateEmailInMirror(Exception):
    """Two distinct FusionAuth user IDs collided on the same email.

    Raised by :func:`upsert_user_mirror` when an INSERT/UPSERT against
    the local ``users`` table trips the case-insensitive email-unique
    index AND the row already occupying that email has a different
    primary key than the one being inserted.

    Attributes:
        email: The lowercased email that collided.
        existing_id: PK of the row already mapped to ``email`` (the
            row already in the DB). ``None`` if the lookup that would
            have populated this attribute also failed.
        attempted_id: PK the caller tried to insert (typically the FA
            ``sub`` claim from the JWT that triggered the mirror).
            ``None`` only when the caller did not have an id in hand.
    """

    def __init__(
        self,
        email: str,
        existing_id: Optional[UUID] = None,
        attempted_id: Optional[UUID] = None,
    ) -> None:
        self.email = email
        self.existing_id = existing_id
        self.attempted_id = attempted_id
        super().__init__(self.__str__())

    def __str__(self) -> str:
        return (
            f"email {self.email!r} already mapped to user "
            f"{self.existing_id} (attempted by {self.attempted_id})"
        )


async def get_user_by_id(
    session: AsyncSession, user_id: UUID
) -> Optional[User]:
    """Fetch the local users mirror row for the given FusionAuth sub.

    Looks up the ``users`` row whose primary key equals ``user_id``
    (which is the FA ``sub`` claim from the validated JWT). Returns
    ``None`` if no row exists — callers (auth router, /me handler)
    use that signal to drive a mirror auto-create flow.

    DB exceptions (connection drop, statement timeout, etc.) are
    intentionally NOT swallowed; they bubble as SQLAlchemy errors so
    the route layer can translate them to ``503 auth_service_unavailable``.

    Args:
        session: Active SQLAlchemy AsyncSession bound to the app DB.
        user_id: FusionAuth ``sub`` claim — the PK of the users table.

    Returns:
        The :class:`User` row, or ``None`` if no such row exists.
    """
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def upsert_user_mirror(
    session: Session,
    fa_user_id: UUID,
    email: str,
    role: str = "user",
    display_name: Optional[str] = None,
) -> User:
    """Insert-if-absent the local users mirror row for a FusionAuth user.

    Concurrency-safe mirror writer used by signup, login, and the
    ``/me`` auto-create path. Issues a Postgres
    ``INSERT ... ON CONFLICT (id) DO NOTHING ... RETURNING *``; if the
    INSERT was skipped because another transaction already mirrored
    the same FA user id (e.g. two concurrent /me calls racing), falls
    back to a plain ``SELECT`` against the same id and returns that
    row. The mirror is intentionally insert-only — ``role``,
    ``display_name``, and ``email`` are NEVER updated on conflict
    because the JWT (for role) and FusionAuth (for email /
    display_name) remain the authoritative sources.

    Email is lowercased BEFORE the INSERT so the case-insensitive
    unique constraint (CITEXT in production, lowercased-string in
    sqlite tests) cannot be tricked by mixed-case input on the
    same address. The caller is free to pass any case.

    Exception contract:

    * :class:`DuplicateEmailInMirror` — raised when the INSERT trips
      the email-unique index (constraint name contains
      ``users_email_key``) but NOT the primary-key constraint. This
      indicates two distinct FA user ids competing for the same email
      — pathological because FA enforces email uniqueness, but the
      mirror still needs a typed signal so the auth router can
      translate to ``500 duplicate_email_in_mirror``. The session is
      rolled back before the exception is raised so the caller's
      transaction is left in a usable state.
    * :class:`sqlalchemy.exc.IntegrityError` — re-raised unchanged if
      the constraint violated is anything other than the email unique
      index (e.g. the primary key, which should be impossible given
      ``ON CONFLICT (id) DO NOTHING``). Defensive: keeps the original
      diagnostic traceback rather than masking it.
    * All other :class:`sqlalchemy.exc.SQLAlchemyError` subclasses
      bubble untouched — connection drops, statement timeouts, etc.
      are translated to ``503`` at the route layer.

    Args:
        session: Active synchronous SQLAlchemy ``Session`` bound to
            the application DB.
        fa_user_id: The FusionAuth ``sub`` claim; becomes the PK of
            the local users row.
        email: User email (any case); will be lowercased before
            persistence.
        role: Snapshot of the JWT roles claim. Defaults to ``'user'``.
            Not updated on conflict — JWT is authoritative for authz.
        display_name: Optional friendly display name, or ``None``.

    Returns:
        The :class:`User` row — either the freshly inserted one (when
        no conflict) or the pre-existing row (when another caller
        beat us to the insert).
    """
    normalized_email = email.lower()

    stmt = (
        pg_insert(User)
        .values(
            id=fa_user_id,
            email=normalized_email,
            role=role,
            display_name=display_name,
        )
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(User)
    )

    try:
        result = session.execute(stmt)
        session.flush()
    except IntegrityError as e:
        # Distinguish the email-unique-constraint violation from the
        # PK violation. The Alembic migration names the constraint
        # ``uq_users_email``; older drivers / naming-convention setups
        # might surface ``users_email_key`` instead. Match either, and
        # fall back to the looser "email + unique" substring so future
        # naming-convention drift doesn't silently propagate the
        # raw IntegrityError up to the route layer.
        orig_text = str(getattr(e, "orig", "") or "")
        orig_lower = orig_text.lower()
        if (
            "uq_users_email" in orig_text
            or "users_email_key" in orig_text
            or ("email" in orig_lower and "unique" in orig_lower)
        ):
            session.rollback()
            raise DuplicateEmailInMirror(
                email=normalized_email,
                attempted_id=fa_user_id,
            )
        # PK collision (or anything else) — defensive re-raise. With
        # ``ON CONFLICT (id) DO NOTHING`` the PK path should be
        # unreachable, but if a future schema change adds another
        # unique index we want the original error to surface so the
        # bug is obvious in logs.
        raise

    new_user = result.scalar_one_or_none()
    if new_user is not None:
        return new_user

    # Conflict path: ON CONFLICT DO NOTHING swallowed the insert and
    # RETURNING produced no rows. Another caller (or this caller in a
    # prior transaction) already mirrored this id, so the row MUST
    # exist now. ``scalar_one`` (not ``scalar_one_or_none``) on
    # purpose — absence here is a programming-error invariant break
    # worth raising on.
    existing = session.execute(
        select(User).where(User.id == fa_user_id)
    ).scalar_one()
    return existing

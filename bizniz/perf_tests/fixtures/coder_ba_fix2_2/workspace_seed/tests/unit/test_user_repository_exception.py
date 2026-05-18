"""Unit tests for the DuplicateEmailInMirror exception class.

Covers attribute storage, the str() formatter shape (which the auth
router will log when translating to ``500 duplicate_email_in_mirror``),
default arg behavior, and the Exception ancestry so callers can
``except Exception`` safely.
"""
import uuid

import pytest

from app.repositories.user_repository import DuplicateEmailInMirror


@pytest.mark.unit
class TestDuplicateEmailInMirrorConstruction:
    """Attribute storage and default-arg semantics."""

    def test_is_exception_subclass(self):
        assert issubclass(DuplicateEmailInMirror, Exception)

    def test_stores_all_three_attributes(self):
        existing = uuid.uuid4()
        attempted = uuid.uuid4()
        exc = DuplicateEmailInMirror(
            email="dup@example.com",
            existing_id=existing,
            attempted_id=attempted,
        )
        assert exc.email == "dup@example.com"
        assert exc.existing_id == existing
        assert exc.attempted_id == attempted

    def test_existing_and_attempted_default_to_none(self):
        exc = DuplicateEmailInMirror(email="dup@example.com")
        assert exc.email == "dup@example.com"
        assert exc.existing_id is None
        assert exc.attempted_id is None

    def test_email_is_positional_or_keyword(self):
        # The exception signature must allow ``email`` as the first
        # positional arg so callers in the repository can raise it
        # without keyword noise.
        exc = DuplicateEmailInMirror("dup@example.com")
        assert exc.email == "dup@example.com"


@pytest.mark.unit
class TestDuplicateEmailInMirrorMessage:
    """str(exc) formatting — used for ERROR-log lines in the router."""

    def test_message_contains_quoted_email_and_both_ids(self):
        existing = uuid.uuid4()
        attempted = uuid.uuid4()
        exc = DuplicateEmailInMirror(
            email="dup@example.com",
            existing_id=existing,
            attempted_id=attempted,
        )
        msg = str(exc)
        assert "'dup@example.com'" in msg
        assert str(existing) in msg
        assert str(attempted) in msg
        assert "already mapped to user" in msg
        assert "attempted by" in msg

    def test_message_format_matches_contract(self):
        existing = uuid.uuid4()
        attempted = uuid.uuid4()
        exc = DuplicateEmailInMirror(
            email="dup@example.com",
            existing_id=existing,
            attempted_id=attempted,
        )
        expected = (
            f"email 'dup@example.com' already mapped to user "
            f"{existing} (attempted by {attempted})"
        )
        assert str(exc) == expected

    def test_message_renders_none_ids_literally(self):
        exc = DuplicateEmailInMirror(email="dup@example.com")
        msg = str(exc)
        assert "'dup@example.com'" in msg
        assert "None" in msg

    def test_exception_args_carry_the_message(self):
        # raising it and stringifying via Exception.__str__ should
        # surface the same message — many loggers go through args.
        existing = uuid.uuid4()
        attempted = uuid.uuid4()
        with pytest.raises(DuplicateEmailInMirror) as excinfo:
            raise DuplicateEmailInMirror(
                email="dup@example.com",
                existing_id=existing,
                attempted_id=attempted,
            )
        # The exception's first arg should be the formatted message,
        # so ``logging.exception`` / ``str(exc)`` agree.
        assert str(excinfo.value.args[0]) == str(excinfo.value)


@pytest.mark.unit
class TestDuplicateEmailInMirrorRaiseAndCatch:
    """The router's except-block must be able to catch it cleanly."""

    def test_can_be_raised_and_caught_as_self(self):
        with pytest.raises(DuplicateEmailInMirror):
            raise DuplicateEmailInMirror(email="x@example.com")

    def test_can_be_caught_as_exception(self):
        with pytest.raises(Exception):
            raise DuplicateEmailInMirror(email="x@example.com")

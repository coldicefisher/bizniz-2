"""Unit tests for CurrentUser model and _pick_role role-precedence helper.

Covers the public contract added in BE-006-U1:
- _ROLE_PRECEDENCE ordering (super_admin > admin > user)
- _pick_role returns highest-precedence role present
- CurrentUser is a Pydantic v2 BaseModel with id/email/display_name/role
"""
import uuid

import pytest
from pydantic import BaseModel, ValidationError

from app.core.auth import _ROLE_PRECEDENCE, CurrentUser, _pick_role


class TestRolePrecedenceConstant:
    def test_precedence_order_is_fixed(self) -> None:
        assert _ROLE_PRECEDENCE == ("super_admin", "admin", "user")

    def test_precedence_is_tuple(self) -> None:
        assert isinstance(_ROLE_PRECEDENCE, tuple)


class TestPickRole:
    def test_super_admin_wins_over_admin_and_user(self) -> None:
        assert _pick_role(["user", "admin", "super_admin"]) == "super_admin"

    def test_admin_wins_over_user(self) -> None:
        assert _pick_role(["user", "admin"]) == "admin"

    def test_user_when_only_user_present(self) -> None:
        assert _pick_role(["user"]) == "user"

    def test_super_admin_alone(self) -> None:
        assert _pick_role(["super_admin"]) == "super_admin"

    def test_admin_alone(self) -> None:
        assert _pick_role(["admin"]) == "admin"

    def test_none_when_empty(self) -> None:
        assert _pick_role([]) is None

    def test_none_when_only_unknown_roles(self) -> None:
        assert _pick_role(["guest", "viewer"]) is None

    def test_unknown_roles_ignored_alongside_known(self) -> None:
        assert _pick_role(["guest", "admin", "viewer"]) == "admin"

    def test_order_in_input_does_not_matter(self) -> None:
        # The precedence is fixed by _ROLE_PRECEDENCE, not by input order.
        assert _pick_role(["admin", "super_admin"]) == "super_admin"
        assert _pick_role(["super_admin", "admin"]) == "super_admin"
        assert _pick_role(["user", "super_admin"]) == "super_admin"

    def test_duplicates_handled(self) -> None:
        assert _pick_role(["user", "user", "admin"]) == "admin"


class TestCurrentUserModel:
    def test_is_pydantic_base_model(self) -> None:
        assert issubclass(CurrentUser, BaseModel)

    def test_minimum_required_fields(self) -> None:
        user_id = uuid.uuid4()
        cu = CurrentUser(id=user_id, email="test@example.com", role="user")
        assert cu.id == user_id
        assert cu.email == "test@example.com"
        assert cu.role == "user"
        assert cu.display_name is None

    def test_with_display_name(self) -> None:
        user_id = uuid.uuid4()
        cu = CurrentUser(
            id=user_id,
            email="admin@example.com",
            display_name="Admin Person",
            role="admin",
        )
        assert cu.display_name == "Admin Person"

    def test_id_must_be_uuid(self) -> None:
        # Pydantic v2 accepts UUID strings and coerces; non-UUID strings fail.
        cu = CurrentUser(
            id="11111111-1111-1111-1111-111111111111",
            email="x@y.z",
            role="user",
        )
        assert isinstance(cu.id, uuid.UUID)

    def test_id_rejects_non_uuid(self) -> None:
        with pytest.raises(ValidationError):
            CurrentUser(id="not-a-uuid", email="x@y.z", role="user")

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            CurrentUser(email="x@y.z", role="user")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            CurrentUser(id=uuid.uuid4(), role="user")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            CurrentUser(id=uuid.uuid4(), email="x@y.z")  # type: ignore[call-arg]

    def test_role_field_is_string(self) -> None:
        cu = CurrentUser(
            id=uuid.uuid4(), email="x@y.z", role="super_admin",
        )
        assert cu.role == "super_admin"
        assert isinstance(cu.role, str)


class TestIntegrationOfPickRoleWithCurrentUser:
    """End-to-end: derive role via _pick_role, hand to CurrentUser."""

    def test_construct_current_user_from_jwt_roles_claim(self) -> None:
        jwt_roles_claim = ["user", "admin"]
        chosen = _pick_role(jwt_roles_claim)
        assert chosen == "admin"
        cu = CurrentUser(
            id=uuid.uuid4(),
            email="user@example.com",
            display_name=None,
            role=chosen,
        )
        assert cu.role == "admin"

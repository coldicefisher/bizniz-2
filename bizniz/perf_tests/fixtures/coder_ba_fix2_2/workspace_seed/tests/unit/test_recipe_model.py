"""Unit tests for the Recipe SQLAlchemy 2.0 model definition.

Tests inspect the declarative model metadata directly — no DB
connection required. JSONB and PG_UUID are Postgres-specific types
and can't be materialised by SQLite, so we verify the schema by
introspecting ``__table__`` instead of running DDL.

DDL (CHECK constraints, indexes) lives in BE-001's migration, NOT
on the model — these tests assert the model carries only column-
level mapping metadata and intentionally has no ``__table_args__``
for CHECK constraints.
"""
import uuid

import pytest
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.sql.functions import now as sql_now

from app.db.base import Base
from app.models.recipe import Recipe


@pytest.mark.unit
class TestRecipeModelStructure:
    """Column-level metadata: name, type, nullability, defaults."""

    def test_tablename_is_recipes(self):
        assert Recipe.__tablename__ == "recipes"

    def test_inherits_from_base(self):
        assert issubclass(Recipe, Base)

    def test_registered_in_metadata(self):
        assert "recipes" in Base.metadata.tables

    def test_has_expected_columns(self):
        expected = {
            "id",
            "owner_id",
            "title",
            "description",
            "ingredients",
            "instructions",
            "prep_time",
            "cook_time",
            "servings",
            "created_at",
            "updated_at",
        }
        assert set(Recipe.__table__.columns.keys()) == expected


@pytest.mark.unit
class TestRecipeIdColumn:
    """``id`` is a Postgres UUID PK with a server-side default."""

    def test_id_is_primary_key_pg_uuid(self):
        col = Recipe.__table__.c.id
        assert col.primary_key is True
        assert isinstance(col.type, PG_UUID)
        assert col.type.as_uuid is True

    def test_id_server_default_is_gen_random_uuid(self):
        col = Recipe.__table__.c.id
        assert col.server_default is not None
        # ``server_default.arg`` is a TextClause for text() defaults.
        assert "gen_random_uuid()" in str(col.server_default.arg)


@pytest.mark.unit
class TestRecipeOwnerIdColumn:
    """``owner_id`` is a NOT NULL FK to ``users(id)`` with CASCADE."""

    def test_owner_id_type_and_nullability(self):
        col = Recipe.__table__.c.owner_id
        assert isinstance(col.type, PG_UUID)
        assert col.type.as_uuid is True
        assert col.nullable is False

    def test_owner_id_has_foreign_key_to_users(self):
        col = Recipe.__table__.c.owner_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1, f"expected 1 FK on owner_id, got {fks}"
        fk = fks[0]
        # ``target_fullname`` is "<table>.<column>" of the referent.
        assert fk.target_fullname == "users.id"

    def test_owner_id_fk_on_delete_cascade(self):
        col = Recipe.__table__.c.owner_id
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "CASCADE"


@pytest.mark.unit
class TestRecipeStringColumns:
    """``title`` and ``description`` are NOT NULL String columns."""

    def test_title_string_not_null(self):
        col = Recipe.__table__.c.title
        assert isinstance(col.type, String)
        assert col.nullable is False

    def test_description_string_not_null(self):
        col = Recipe.__table__.c.description
        assert isinstance(col.type, String)
        assert col.nullable is False


@pytest.mark.unit
class TestRecipeJsonbColumns:
    """``ingredients`` and ``instructions`` are NOT NULL JSONB."""

    def test_ingredients_jsonb_not_null(self):
        col = Recipe.__table__.c.ingredients
        assert isinstance(col.type, JSONB)
        assert col.nullable is False

    def test_instructions_jsonb_not_null(self):
        col = Recipe.__table__.c.instructions
        assert isinstance(col.type, JSONB)
        assert col.nullable is False


@pytest.mark.unit
class TestRecipeIntegerColumns:
    """``prep_time``, ``cook_time``, ``servings`` are NOT NULL Integer."""

    @pytest.mark.parametrize(
        "column_name",
        ["prep_time", "cook_time", "servings"],
    )
    def test_integer_column(self, column_name: str):
        col = Recipe.__table__.c[column_name]
        assert isinstance(col.type, Integer)
        assert col.nullable is False


@pytest.mark.unit
class TestRecipeTimestampColumns:
    """``created_at`` / ``updated_at`` are timestamptz with server now()."""

    def test_created_at_timestamptz_server_default_now(self):
        col = Recipe.__table__.c.created_at
        assert isinstance(col.type, DateTime)
        assert col.type.timezone is True
        assert col.nullable is False
        assert col.server_default is not None
        assert isinstance(col.server_default.arg, sql_now)
        # created_at must NOT bump on UPDATE.
        assert col.onupdate is None

    def test_updated_at_timestamptz_server_default_and_onupdate_now(self):
        col = Recipe.__table__.c.updated_at
        assert isinstance(col.type, DateTime)
        assert col.type.timezone is True
        assert col.nullable is False
        assert col.server_default is not None
        assert isinstance(col.server_default.arg, sql_now)
        assert col.onupdate is not None
        assert isinstance(col.onupdate.arg, sql_now)


@pytest.mark.unit
class TestRecipeNoTableArgs:
    """Model carries no DDL — CHECK constraints live in the migration.

    BE-001's migration owns CHECK constraints (length bounds, time
    bounds, servings bound). The model must not duplicate them via
    ``__table_args__`` — duplicated DDL drifts.
    """

    def test_no_check_constraints_on_model(self):
        from sqlalchemy import CheckConstraint

        cks = [
            c
            for c in Recipe.__table__.constraints
            if isinstance(c, CheckConstraint)
        ]
        assert cks == [], (
            f"Recipe model should not declare CHECK constraints "
            f"(migration owns DDL); found: {cks}"
        )


@pytest.mark.unit
class TestRecipeInstantiation:
    """The model must be instantiable with the documented kwargs."""

    def test_can_instantiate_with_all_fields(self):
        rid = uuid.uuid4()
        oid = uuid.uuid4()
        r = Recipe(
            id=rid,
            owner_id=oid,
            title="Pancakes",
            description="Fluffy pancakes for two.",
            ingredients=["flour", "milk", "egg"],
            instructions=["mix", "cook", "serve"],
            prep_time=10,
            cook_time=15,
            servings=2,
        )
        assert r.id == rid
        assert r.owner_id == oid
        assert r.title == "Pancakes"
        assert r.description == "Fluffy pancakes for two."
        assert r.ingredients == ["flour", "milk", "egg"]
        assert r.instructions == ["mix", "cook", "serve"]
        assert r.prep_time == 10
        assert r.cook_time == 15
        assert r.servings == 2

    def test_can_instantiate_without_server_managed_fields(self):
        """``id``, ``created_at``, ``updated_at`` are server-managed.

        The model must accept construction without those values
        (they'd be filled in by Postgres on flush).
        """
        r = Recipe(
            owner_id=uuid.uuid4(),
            title="Toast",
            description="Bread, but warm.",
            ingredients=["bread"],
            instructions=["toast it"],
            prep_time=0,
            cook_time=2,
            servings=1,
        )
        assert r.title == "Toast"

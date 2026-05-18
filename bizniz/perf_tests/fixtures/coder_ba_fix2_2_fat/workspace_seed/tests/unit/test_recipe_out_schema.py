"""Unit tests for app.schemas.recipe.RecipeOut (BE-003-U3).

RecipeOut is a pure response-shape Pydantic model that serializes a
SQLAlchemy Recipe instance directly via ``from_attributes=True``. There
are no validators or field constraints — these tests assert the wire
contract (field names, types, ORM-instance compatibility, dict
compatibility, JSON round-trip).
"""
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from app.schemas.recipe import RecipeOut


def _orm_recipe_dict(**overrides) -> dict:
    """Build a dict shaped like a Recipe ORM instance, overrideable."""
    payload = {
        "id": uuid4(),
        "owner_id": uuid4(),
        "title": "Pancakes",
        "description": "Fluffy weekend pancakes.",
        "ingredients": ["flour", "milk", "eggs"],
        "instructions": ["mix", "cook", "serve"],
        "prep_time": 10,
        "cook_time": 15,
        "servings": 4,
        "created_at": datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 17, 12, 0, 1, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return payload


class _FakeRecipeORM:
    """Minimal attribute-bearing stand-in for a SQLAlchemy Recipe row.

    RecipeOut declares ``from_attributes=True``, so it must accept any
    object whose attributes match its field names — we don't need a
    real SQLAlchemy instance to exercise that path.
    """

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


@pytest.mark.unit
class TestRecipeOutHappyPath:
    def test_subclass_of_basemodel(self):
        assert issubclass(RecipeOut, BaseModel)

    def test_construct_from_dict(self):
        data = _orm_recipe_dict()
        out = RecipeOut(**data)
        assert out.id == data["id"]
        assert out.owner_id == data["owner_id"]
        assert out.title == "Pancakes"
        assert out.description == "Fluffy weekend pancakes."
        assert out.ingredients == ["flour", "milk", "eggs"]
        assert out.instructions == ["mix", "cook", "serve"]
        assert out.prep_time == 10
        assert out.cook_time == 15
        assert out.servings == 4
        assert out.created_at == data["created_at"]
        assert out.updated_at == data["updated_at"]

    def test_model_validate_from_dict(self):
        data = _orm_recipe_dict()
        out = RecipeOut.model_validate(data)
        assert out.title == data["title"]

    def test_model_validate_from_orm_like_object(self):
        data = _orm_recipe_dict()
        orm = _FakeRecipeORM(**data)
        out = RecipeOut.model_validate(orm)
        assert out.id == data["id"]
        assert out.owner_id == data["owner_id"]
        assert out.title == data["title"]
        assert out.description == data["description"]
        assert out.ingredients == data["ingredients"]
        assert out.instructions == data["instructions"]
        assert out.prep_time == data["prep_time"]
        assert out.cook_time == data["cook_time"]
        assert out.servings == data["servings"]
        assert out.created_at == data["created_at"]
        assert out.updated_at == data["updated_at"]


@pytest.mark.unit
class TestRecipeOutFieldTypes:
    def test_id_is_uuid_type(self):
        data = _orm_recipe_dict()
        out = RecipeOut(**data)
        assert isinstance(out.id, UUID)

    def test_owner_id_is_uuid_type(self):
        data = _orm_recipe_dict()
        out = RecipeOut(**data)
        assert isinstance(out.owner_id, UUID)

    def test_created_at_is_datetime(self):
        data = _orm_recipe_dict()
        out = RecipeOut(**data)
        assert isinstance(out.created_at, datetime)

    def test_updated_at_is_datetime(self):
        data = _orm_recipe_dict()
        out = RecipeOut(**data)
        assert isinstance(out.updated_at, datetime)

    def test_uuid_string_accepted(self):
        # Pydantic will coerce a UUID string into UUID even without strict mode.
        rid = "11111111-1111-1111-1111-111111111111"
        oid = "22222222-2222-2222-2222-222222222222"
        out = RecipeOut(**_orm_recipe_dict(id=rid, owner_id=oid))
        assert out.id == UUID(rid)
        assert out.owner_id == UUID(oid)


@pytest.mark.unit
class TestRecipeOutSchemaShape:
    def test_has_all_expected_fields(self):
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
        assert set(RecipeOut.model_fields.keys()) == expected

    def test_from_attributes_config_enabled(self):
        # Load-bearing: the route layer relies on this to serialize an
        # ORM instance directly without dumping it to a dict first.
        assert RecipeOut.model_config.get("from_attributes") is True

    def test_no_extra_forbid_config(self):
        # RecipeOut is a response shape, not a request schema; it does
        # NOT carry the strict / extra=forbid config that RecipeCreate
        # does. Verify we did not accidentally inherit that.
        cfg = RecipeOut.model_config
        assert cfg.get("extra") != "forbid"
        assert cfg.get("strict") is not True


@pytest.mark.unit
class TestRecipeOutSerialization:
    def test_model_dump_returns_dict(self):
        data = _orm_recipe_dict()
        out = RecipeOut(**data)
        dumped = out.model_dump()
        assert dumped["title"] == data["title"]
        assert dumped["ingredients"] == data["ingredients"]
        assert dumped["instructions"] == data["instructions"]

    def test_model_dump_json_serializes_uuid_and_datetime(self):
        data = _orm_recipe_dict()
        out = RecipeOut(**data)
        json_str = out.model_dump_json()
        # UUID and datetime must render as JSON strings (not raise).
        assert str(data["id"]) in json_str
        assert "2026-05-17" in json_str

    def test_unicode_round_trip(self):
        data = _orm_recipe_dict(
            title="Soupe à l'oignon 🧅",
            description="Très bon — おいしい",
            ingredients=["oignon", "🧄"],
            instructions=["émincer", "cuire"],
        )
        out = RecipeOut(**data)
        dumped = out.model_dump()
        assert dumped["title"] == "Soupe à l'oignon 🧅"
        assert dumped["description"] == "Très bon — おいしい"
        assert dumped["ingredients"] == ["oignon", "🧄"]
        assert dumped["instructions"] == ["émincer", "cuire"]


@pytest.mark.unit
class TestRecipeOutWithRecipeModel:
    """Wire RecipeOut against the real SQLAlchemy Recipe class.

    We don't hit the DB — we construct an unbound Recipe instance with
    attribute assignment and feed it to ``RecipeOut.model_validate``.
    This exercises the ``from_attributes=True`` path with the actual
    ORM class the route layer will hand it.
    """

    def test_model_validate_real_recipe_instance(self):
        from app.models.recipe import Recipe

        data = _orm_recipe_dict()
        recipe = Recipe()
        for k, v in data.items():
            setattr(recipe, k, v)

        out = RecipeOut.model_validate(recipe)
        assert out.id == data["id"]
        assert out.owner_id == data["owner_id"]
        assert out.title == data["title"]
        assert out.ingredients == data["ingredients"]
        assert out.created_at == data["created_at"]
        assert out.updated_at == data["updated_at"]

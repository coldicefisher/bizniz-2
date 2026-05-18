"""Unit tests for app.schemas.recipe.RecipeSummary (BE-003-U4).

RecipeSummary is a subclass of RecipeOut. For this milestone the two
share the same field set; the subclass exists so the list endpoint can
reference a distinct OpenAPI schema name that a future milestone can
narrow without renaming.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.recipe import RecipeOut, RecipeSummary


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


@pytest.mark.unit
class TestRecipeSummaryIdentity:
    def test_is_subclass_of_recipe_out(self):
        # The whole point of subclassing (vs aliasing) is that
        # RecipeSummary is a distinct type that IS-A RecipeOut.
        assert issubclass(RecipeSummary, RecipeOut)

    def test_is_distinct_class_from_recipe_out(self):
        # Subclass, not alias — the OpenAPI schema name needs to differ.
        assert RecipeSummary is not RecipeOut

    def test_class_name_is_recipe_summary(self):
        assert RecipeSummary.__name__ == "RecipeSummary"


@pytest.mark.unit
class TestRecipeSummaryFieldSet:
    def test_field_set_matches_recipe_out(self):
        # Shape parity is intentional for this milestone (see module
        # docstring on app/schemas/recipe.py).
        assert set(RecipeSummary.model_fields.keys()) == set(
            RecipeOut.model_fields.keys()
        )

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
        assert set(RecipeSummary.model_fields.keys()) == expected

    def test_from_attributes_inherited(self):
        # Required so the route layer can pass a SQLAlchemy Recipe
        # instance straight into ``RecipeSummary.model_validate``.
        assert RecipeSummary.model_config.get("from_attributes") is True


@pytest.mark.unit
class TestRecipeSummaryConstruction:
    def test_construct_from_dict(self):
        data = _orm_recipe_dict()
        summary = RecipeSummary(**data)
        assert summary.id == data["id"]
        assert summary.owner_id == data["owner_id"]
        assert summary.title == "Pancakes"
        assert summary.description == "Fluffy weekend pancakes."
        assert summary.ingredients == ["flour", "milk", "eggs"]
        assert summary.instructions == ["mix", "cook", "serve"]
        assert summary.prep_time == 10
        assert summary.cook_time == 15
        assert summary.servings == 4
        assert summary.created_at == data["created_at"]
        assert summary.updated_at == data["updated_at"]

    def test_model_validate_from_dict(self):
        data = _orm_recipe_dict()
        summary = RecipeSummary.model_validate(data)
        assert summary.title == data["title"]

    def test_model_validate_from_orm_like_object(self):
        class _FakeRecipeORM:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        data = _orm_recipe_dict()
        orm = _FakeRecipeORM(**data)
        summary = RecipeSummary.model_validate(orm)
        assert summary.id == data["id"]
        assert summary.title == data["title"]
        assert summary.ingredients == data["ingredients"]

    def test_model_dump_returns_full_field_set(self):
        data = _orm_recipe_dict()
        summary = RecipeSummary(**data)
        dumped = summary.model_dump()
        for key in (
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
        ):
            assert key in dumped

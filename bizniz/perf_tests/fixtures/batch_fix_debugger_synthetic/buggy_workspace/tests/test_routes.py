"""Synthetic test that fails because models.py is missing the tags field.

Intentional defect: the assertion at the end is correct per the
contract — it must NOT be relaxed. Fix the model + route, not the test.
"""
from __future__ import annotations

from app.models import Recipe
from app.routes import get_recipe_response


def test_recipe_response_includes_tags():
    r = Recipe(
        id="r-1",
        title="Pancakes",
        owner_id="u-1",
        tags=["breakfast", "easy"],  # AttributeError: no such field
    )
    body = get_recipe_response(r)
    assert body["tags"] == ["breakfast", "easy"]


def test_recipe_response_includes_id_and_title():
    r = Recipe(id="r-2", title="Toast", owner_id="u-1", tags=[])
    body = get_recipe_response(r)
    assert body["id"] == "r-2"
    assert body["title"] == "Toast"

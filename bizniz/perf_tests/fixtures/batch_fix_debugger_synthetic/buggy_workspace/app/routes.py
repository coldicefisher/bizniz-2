"""HTTP route handlers for the synthetic test.

Intentional defects:
  - Imports ``JWTError`` unused (ruff: F401)
  - References ``recipe.tags`` which doesn't exist on Recipe yet
    (mypy: attr-defined). Will be fixed when models.py adds tags.
  - ``create_recipe`` swallows exceptions silently (CR finding).
"""
from __future__ import annotations

from typing import Dict, List

from jose import JWTError  # unused — ruff F401

from app.models import Recipe


def get_recipe_response(recipe: Recipe) -> Dict[str, object]:
    """Build the API response body for a Recipe."""
    return {
        "id": recipe.id,
        "title": recipe.title,
        "owner_id": recipe.owner_id,
        "tags": recipe.tags,  # mypy: Recipe has no attr 'tags'
    }


def create_recipe(payload: Dict[str, object]) -> Recipe:
    """Create a recipe. Swallows ALL exceptions silently — bad."""
    try:
        return Recipe(
            id=str(payload["id"]),
            title=str(payload["title"]),
            owner_id=str(payload["owner_id"]),
        )
    except Exception:
        # CR finding: swallowed exception, no logging, returns None
        # (return type lies — should be Recipe).
        return None  # type: ignore[return-value]

"""Pydantic schemas for Recipe Box recipe requests and responses.

Defines the wire-format shapes the recipe endpoints accept and return.
Request schemas (e.g. RecipeCreate) enforce field constraints with
Pydantic v2 ``Field(...)`` and strict mode so that integer fields
reject floats and strings, and unknown body fields are rejected as
400 rather than silently dropped.

NOTE on RecipeSummary vs RecipeOut: for this milestone, RecipeSummary
intentionally mirrors RecipeOut (no field reduction). The list and
detail endpoints return the same projection. The summary type exists
as a distinct symbol so a future milestone can narrow it (drop
``description`` / large array fields for list views) without churning
every call site. Today they are identical by design.
"""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RecipeCreate(BaseModel):
    """Request body for POST /api/recipes.

    Field constraints mirror the create_recipe capability contract:
    title 1-200 chars, description 1-5000 chars, ingredients and
    instructions 1-100 items each, prep_time/cook_time 0-1440 minutes
    (24h cap), servings 1-1000.

    ``model_config``:
    - ``extra='forbid'`` — unknown fields raise a validation error
      (route layer translates to 400). Protects against accidental
      client-supplied owner_id / id / created_at, and against typos
      in field names (e.g. ``tite`` silently dropped).
    - ``str_strip_whitespace=True`` — strings are trimmed before
      length validation runs, so a whitespace-only ``title`` becomes
      ``""`` and fails ``min_length=1``.
    - ``strict=True`` — disables coercion globally for this schema.
      Required so int fields reject floats (``3.0`` → reject, not
      silently coerce to ``3``) and strings (``"5"`` → reject) per
      the contract's ``integer_strict`` test scenario.

    Per-item validation for the ingredient / instruction string
    lists (each item 1-300 chars / 1-2000 chars, no whitespace-only
    entries) is enforced via ``@field_validator`` methods that run
    after the Field-level outer constraints.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        strict=True,
    )

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    ingredients: list[str] = Field(min_length=1, max_length=100)
    instructions: list[str] = Field(min_length=1, max_length=100)
    prep_time: int = Field(ge=0, le=1440)
    cook_time: int = Field(ge=0, le=1440)
    servings: int = Field(ge=1, le=1000)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        """Reject titles that contain newlines or are empty after trim.

        ``str_strip_whitespace=True`` trims leading/trailing whitespace
        before this runs, but embedded newlines survive that trim and
        would otherwise pass ``min_length=1``. Title is rendered as a
        single line in the UI, so an embedded ``\\n`` or ``\\r`` is
        rejected here.
        """
        if "\n" in v or "\r" in v:
            raise ValueError("title must be a single line")
        # Defensive re-check: Field(min_length=1) under
        # str_strip_whitespace=True already rejects whitespace-only
        # inputs, but re-check here so this validator is correct
        # regardless of config drift.
        if not v.strip():
            raise ValueError("title must not be empty after trim")
        return v

    @field_validator("ingredients")
    @classmethod
    def _validate_ingredients(cls, v: list[str]) -> list[str]:
        """Trim each ingredient; reject empty entries or items > 300 chars.

        Preserves order (duplicates are allowed — the contract says
        ``['salt', 'salt']`` is accepted; user intent is preserved).
        """
        cleaned: list[str] = []
        for item in v:
            trimmed = item.strip()
            if not trimmed:
                raise ValueError(
                    "ingredient item must not be empty after trim"
                )
            if len(trimmed) > 300:
                raise ValueError(
                    "ingredient item must be at most 300 characters"
                )
            cleaned.append(trimmed)
        return cleaned

    @field_validator("instructions")
    @classmethod
    def _validate_instructions(cls, v: list[str]) -> list[str]:
        """Trim each instruction; reject empty entries or items > 2000 chars.

        Preserves order — instruction step order is semantically
        meaningful in the contract.
        """
        cleaned: list[str] = []
        for item in v:
            trimmed = item.strip()
            if not trimmed:
                raise ValueError(
                    "instruction item must not be empty after trim"
                )
            if len(trimmed) > 2000:
                raise ValueError(
                    "instruction item must be at most 2000 characters"
                )
            cleaned.append(trimmed)
        return cleaned


class RecipeOut(BaseModel):
    """Response shape for a single recipe (GET / POST / PUT result).

    Pure response projection — no validators, no constraints. The route
    layer constructs this directly from a SQLAlchemy ``Recipe`` instance
    via ``RecipeOut.model_validate(recipe)``; ``from_attributes=True``
    tells Pydantic to read attributes off the ORM instance rather than
    expecting a dict.

    Field order mirrors the create_recipe capability output contract:
    id, owner_id, title, description, ingredients, instructions,
    prep_time, cook_time, servings, created_at, updated_at.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    title: str
    description: str
    ingredients: list[str]
    instructions: list[str]
    prep_time: int
    cook_time: int
    servings: int
    created_at: datetime
    updated_at: datetime


class RecipeSummary(RecipeOut):
    """Response shape for entries in the GET /api/recipes/mine list.

    Subclasses ``RecipeOut`` to share the same field set today while
    keeping a distinct OpenAPI schema name. The module docstring
    explains the intentional shape parity for this milestone; a
    future milestone can narrow this projection (e.g. drop
    ``description`` and the large list fields) without renaming the
    response type at call sites.
    """

    pass

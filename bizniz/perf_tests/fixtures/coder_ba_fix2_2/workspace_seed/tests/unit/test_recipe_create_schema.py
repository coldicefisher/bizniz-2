"""Unit tests for app.schemas.recipe.RecipeCreate (BE-003-U1).

Covers the field constraints set on the model:
- title / description length bounds
- ingredients / instructions array length bounds
- prep_time / cook_time integer range (0..1440)
- servings integer range (1..1000)
- ``extra='forbid'`` rejects unknown fields
- ``str_strip_whitespace=True`` trims string inputs before validation
- ``strict=True`` rejects float/string in integer fields

Per-item validation for the list-of-string fields is U2 work; this
test module only asserts the U1 constraints.
"""
import pytest
from pydantic import ValidationError

from app.schemas.recipe import RecipeCreate


def _valid_payload(**overrides):
    """Build a known-valid RecipeCreate payload, overrideable per-test."""
    payload = {
        "title": "Pancakes",
        "description": "Fluffy weekend pancakes.",
        "ingredients": ["flour", "milk", "eggs"],
        "instructions": ["mix", "cook", "serve"],
        "prep_time": 10,
        "cook_time": 15,
        "servings": 4,
    }
    payload.update(overrides)
    return payload


@pytest.mark.unit
class TestRecipeCreateHappyPath:
    def test_valid_payload_parses(self):
        rc = RecipeCreate(**_valid_payload())
        assert rc.title == "Pancakes"
        assert rc.description == "Fluffy weekend pancakes."
        assert rc.ingredients == ["flour", "milk", "eggs"]
        assert rc.instructions == ["mix", "cook", "serve"]
        assert rc.prep_time == 10
        assert rc.cook_time == 15
        assert rc.servings == 4

    def test_prep_and_cook_zero_accepted(self):
        rc = RecipeCreate(**_valid_payload(prep_time=0, cook_time=0))
        assert rc.prep_time == 0
        assert rc.cook_time == 0

    def test_servings_one_accepted(self):
        rc = RecipeCreate(**_valid_payload(servings=1))
        assert rc.servings == 1

    def test_boundary_max_values_accepted(self):
        rc = RecipeCreate(
            **_valid_payload(
                title="t" * 200,
                description="d" * 5000,
                prep_time=1440,
                cook_time=1440,
                servings=1000,
            )
        )
        assert len(rc.title) == 200
        assert len(rc.description) == 5000
        assert rc.prep_time == 1440
        assert rc.cook_time == 1440
        assert rc.servings == 1000

    def test_boundary_max_list_sizes_accepted(self):
        rc = RecipeCreate(
            **_valid_payload(
                ingredients=["x"] * 100,
                instructions=["y"] * 100,
            )
        )
        assert len(rc.ingredients) == 100
        assert len(rc.instructions) == 100

    def test_unicode_preserved(self):
        rc = RecipeCreate(
            **_valid_payload(
                title="Soupe à l'oignon 🧅",
                description="Très bon — おいしい",
                ingredients=["oignon", "🧄"],
                instructions=["émincer", "cuire"],
            )
        )
        assert rc.title == "Soupe à l'oignon 🧅"
        assert rc.description == "Très bon — おいしい"
        assert rc.ingredients == ["oignon", "🧄"]
        assert rc.instructions == ["émincer", "cuire"]


@pytest.mark.unit
class TestRecipeCreateTitle:
    def test_empty_title_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(title=""))
        assert any(e["loc"] == ("title",) for e in exc_info.value.errors())

    def test_whitespace_only_title_rejected_after_trim(self):
        # str_strip_whitespace=True turns "   " into "" before min_length runs
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(title="    "))
        assert any(e["loc"] == ("title",) for e in exc_info.value.errors())

    def test_title_trimmed(self):
        rc = RecipeCreate(**_valid_payload(title="  Pancakes  "))
        assert rc.title == "Pancakes"

    def test_title_over_200_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(title="t" * 201))
        assert any(e["loc"] == ("title",) for e in exc_info.value.errors())

    def test_missing_title_rejected(self):
        payload = _valid_payload()
        del payload["title"]
        with pytest.raises(ValidationError):
            RecipeCreate(**payload)


@pytest.mark.unit
class TestRecipeCreateDescription:
    def test_empty_description_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(description=""))
        assert any(e["loc"] == ("description",) for e in exc_info.value.errors())

    def test_description_over_5000_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(description="d" * 5001))
        assert any(e["loc"] == ("description",) for e in exc_info.value.errors())

    def test_description_trimmed(self):
        rc = RecipeCreate(**_valid_payload(description="  hello  "))
        assert rc.description == "hello"

    def test_missing_description_rejected(self):
        payload = _valid_payload()
        del payload["description"]
        with pytest.raises(ValidationError):
            RecipeCreate(**payload)


@pytest.mark.unit
class TestRecipeCreateLists:
    def test_empty_ingredients_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(ingredients=[]))
        assert any(e["loc"] == ("ingredients",) for e in exc_info.value.errors())

    def test_empty_instructions_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(instructions=[]))
        assert any(e["loc"] == ("instructions",) for e in exc_info.value.errors())

    def test_ingredients_over_100_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(ingredients=["x"] * 101))
        assert any(e["loc"] == ("ingredients",) for e in exc_info.value.errors())

    def test_instructions_over_100_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(instructions=["x"] * 101))
        assert any(e["loc"] == ("instructions",) for e in exc_info.value.errors())

    def test_missing_ingredients_rejected(self):
        payload = _valid_payload()
        del payload["ingredients"]
        with pytest.raises(ValidationError):
            RecipeCreate(**payload)


@pytest.mark.unit
class TestRecipeCreateIntegerFields:
    def test_negative_prep_time_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(prep_time=-1))
        assert any(e["loc"] == ("prep_time",) for e in exc_info.value.errors())

    def test_prep_time_over_1440_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(prep_time=1441))
        assert any(e["loc"] == ("prep_time",) for e in exc_info.value.errors())

    def test_negative_cook_time_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(cook_time=-1))
        assert any(e["loc"] == ("cook_time",) for e in exc_info.value.errors())

    def test_cook_time_over_1440_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(cook_time=1441))
        assert any(e["loc"] == ("cook_time",) for e in exc_info.value.errors())

    def test_servings_zero_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(servings=0))
        assert any(e["loc"] == ("servings",) for e in exc_info.value.errors())

    def test_servings_negative_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(servings=-1))
        assert any(e["loc"] == ("servings",) for e in exc_info.value.errors())

    def test_servings_over_1000_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(servings=1001))
        assert any(e["loc"] == ("servings",) for e in exc_info.value.errors())


@pytest.mark.unit
class TestRecipeCreateStrictMode:
    def test_float_in_prep_time_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(prep_time=5.0))
        assert any(e["loc"] == ("prep_time",) for e in exc_info.value.errors())

    def test_float_in_cook_time_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(cook_time=15.5))
        assert any(e["loc"] == ("cook_time",) for e in exc_info.value.errors())

    def test_float_in_servings_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(servings=4.0))
        assert any(e["loc"] == ("servings",) for e in exc_info.value.errors())

    def test_string_in_integer_field_rejected(self):
        # contract: integer_strict — '5' must NOT be coerced
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(prep_time="5"))
        assert any(e["loc"] == ("prep_time",) for e in exc_info.value.errors())


@pytest.mark.unit
class TestRecipeCreateExtraFields:
    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(tags=["dinner"]))
        # extra='forbid' surfaces the offending field name in the loc tuple
        assert any("tags" in e["loc"] for e in exc_info.value.errors())

    def test_client_supplied_owner_id_rejected(self):
        # owner_id must come from JWT; including it in the body is a 400.
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(
                **_valid_payload(owner_id="00000000-0000-0000-0000-000000000000")
            )
        assert any("owner_id" in e["loc"] for e in exc_info.value.errors())

    def test_client_supplied_id_rejected(self):
        with pytest.raises(ValidationError):
            RecipeCreate(**_valid_payload(id="some-id"))

    def test_client_supplied_created_at_rejected(self):
        with pytest.raises(ValidationError):
            RecipeCreate(**_valid_payload(created_at="2026-01-01T00:00:00Z"))


@pytest.mark.unit
class TestRecipeCreateModelConfig:
    def test_model_config_flags_set(self):
        # Sanity-check the config — these flags are load-bearing for the
        # behaviors the route layer depends on.
        cfg = RecipeCreate.model_config
        assert cfg.get("extra") == "forbid"
        assert cfg.get("str_strip_whitespace") is True
        assert cfg.get("strict") is True


@pytest.mark.unit
class TestRecipeCreateTitleNewlines:
    """U2: title must be a single line — embedded \\n / \\r rejected."""

    def test_title_with_newline_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(title="line one\nline two"))
        assert any(e["loc"] == ("title",) for e in exc_info.value.errors())

    def test_title_with_carriage_return_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(title="line one\rline two"))
        assert any(e["loc"] == ("title",) for e in exc_info.value.errors())

    def test_title_with_crlf_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(title="line one\r\nline two"))
        assert any(e["loc"] == ("title",) for e in exc_info.value.errors())

    def test_title_with_leading_newline_rejected(self):
        # str_strip_whitespace trims surrounding whitespace including \n,
        # so a title that's only "\nfoo" trims to "foo" — but an embedded
        # newline in the middle survives and must be rejected.
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(title="foo\nbar"))
        assert any(e["loc"] == ("title",) for e in exc_info.value.errors())

    def test_title_error_message_mentions_single_line(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(title="a\nb"))
        msgs = [e["msg"] for e in exc_info.value.errors() if e["loc"] == ("title",)]
        assert any("single line" in m for m in msgs)


@pytest.mark.unit
class TestRecipeCreateIngredientsPerItem:
    """U2: per-item trim / non-empty / max-300 validation."""

    def test_ingredients_items_are_trimmed(self):
        rc = RecipeCreate(
            **_valid_payload(ingredients=["  flour  ", " milk ", "eggs"])
        )
        assert rc.ingredients == ["flour", "milk", "eggs"]

    def test_ingredients_order_preserved_after_trim(self):
        rc = RecipeCreate(
            **_valid_payload(
                ingredients=[" c ", " a ", " b "],
            )
        )
        assert rc.ingredients == ["c", "a", "b"]

    def test_ingredients_duplicates_preserved(self):
        # Contract says ['salt', 'salt'] is accepted — no dedup.
        rc = RecipeCreate(**_valid_payload(ingredients=["salt", "salt"]))
        assert rc.ingredients == ["salt", "salt"]

    def test_whitespace_only_ingredient_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(ingredients=["flour", "   ", "eggs"]))
        assert any("ingredients" in e["loc"] for e in exc_info.value.errors())

    def test_empty_string_ingredient_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(ingredients=["flour", "", "eggs"]))
        assert any("ingredients" in e["loc"] for e in exc_info.value.errors())

    def test_ingredient_at_300_chars_accepted(self):
        rc = RecipeCreate(**_valid_payload(ingredients=["x" * 300]))
        assert rc.ingredients == ["x" * 300]

    def test_ingredient_over_300_chars_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(ingredients=["x" * 301]))
        assert any("ingredients" in e["loc"] for e in exc_info.value.errors())

    def test_ingredient_over_300_after_trim_rejected(self):
        # "  " + 301 chars + "  " trims to 301 chars — still rejected.
        item = "  " + ("x" * 301) + "  "
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(ingredients=[item]))
        assert any("ingredients" in e["loc"] for e in exc_info.value.errors())

    def test_ingredient_unicode_preserved_after_trim(self):
        rc = RecipeCreate(**_valid_payload(ingredients=["  🧅 oignon  "]))
        assert rc.ingredients == ["🧅 oignon"]


@pytest.mark.unit
class TestRecipeCreateInstructionsPerItem:
    """U2: per-item trim / non-empty / max-2000 validation."""

    def test_instructions_items_are_trimmed(self):
        rc = RecipeCreate(
            **_valid_payload(instructions=["  mix  ", " cook ", "serve"])
        )
        assert rc.instructions == ["mix", "cook", "serve"]

    def test_instructions_order_preserved_after_trim(self):
        # Instruction step order is semantically meaningful.
        rc = RecipeCreate(
            **_valid_payload(
                instructions=[" step one ", " step two ", " step three "],
            )
        )
        assert rc.instructions == ["step one", "step two", "step three"]

    def test_whitespace_only_instruction_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(instructions=["mix", "\t \t", "serve"]))
        assert any("instructions" in e["loc"] for e in exc_info.value.errors())

    def test_empty_string_instruction_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(instructions=["mix", "", "serve"]))
        assert any("instructions" in e["loc"] for e in exc_info.value.errors())

    def test_instruction_at_2000_chars_accepted(self):
        rc = RecipeCreate(**_valid_payload(instructions=["y" * 2000]))
        assert rc.instructions == ["y" * 2000]

    def test_instruction_over_2000_chars_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(instructions=["y" * 2001]))
        assert any("instructions" in e["loc"] for e in exc_info.value.errors())

    def test_instruction_over_2000_after_trim_rejected(self):
        item = "  " + ("y" * 2001) + "  "
        with pytest.raises(ValidationError) as exc_info:
            RecipeCreate(**_valid_payload(instructions=[item]))
        assert any("instructions" in e["loc"] for e in exc_info.value.errors())

    def test_instruction_newlines_inside_item_allowed(self):
        # Per the contract: description and instruction lines may contain
        # newlines (only title is single-line). Newline-containing
        # instructions should pass.
        rc = RecipeCreate(
            **_valid_payload(instructions=["step 1\nsub-step", "step 2"])
        )
        assert rc.instructions == ["step 1\nsub-step", "step 2"]

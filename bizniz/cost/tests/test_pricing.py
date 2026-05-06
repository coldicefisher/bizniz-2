"""Tests for bizniz.cost.pricing."""
import pytest

from bizniz.cost.pricing import MODEL_PRICING, price_call, resolve_model


def test_resolve_model_aliases_gemini():
    assert resolve_model("gemini-flash-lite") == "gemini-2.5-flash-lite"
    assert resolve_model("gemini-flash") == "gemini-3.1-flash-lite-preview"
    assert resolve_model("gemini-pro") == "gemini-3.1-pro-preview"


def test_resolve_model_passes_unknown_through():
    # Non-aliased names go through unchanged so they hit MODEL_PRICING directly.
    assert resolve_model("gpt-4o") == "gpt-4o"
    assert resolve_model("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert resolve_model("totally-unknown-model") == "totally-unknown-model"


def test_price_call_known_model():
    cost = price_call("gpt-4o-mini", input_tokens=1_000_000, output_tokens=500_000)
    assert cost.priced is True
    assert cost.model == "gpt-4o-mini"
    # gpt-4o-mini: $0.15/M input, $0.60/M output
    assert abs(cost.input_cost - 0.15) < 1e-9
    assert abs(cost.output_cost - 0.30) < 1e-9
    assert abs(cost.total_cost - 0.45) < 1e-9


def test_price_call_resolves_alias():
    cost = price_call("gemini-flash-lite", input_tokens=2_000_000, output_tokens=1_000_000)
    assert cost.priced is True
    # alias resolves to gemini-2.5-flash-lite: $0.10/M in, $0.40/M out
    assert cost.model == "gemini-2.5-flash-lite"
    assert abs(cost.input_cost - 0.20) < 1e-9
    assert abs(cost.output_cost - 0.40) < 1e-9


def test_price_call_unknown_model_returns_zero_unpriced():
    cost = price_call("a-model-we-dont-track", input_tokens=10_000, output_tokens=5_000)
    assert cost.priced is False
    assert cost.input_cost == 0.0
    assert cost.output_cost == 0.0
    assert cost.total_cost == 0.0


def test_price_call_zero_tokens():
    cost = price_call("gpt-4o", input_tokens=0, output_tokens=0)
    assert cost.priced is True
    assert cost.total_cost == 0.0


def test_price_call_image_count_charges_per_image():
    """Image-output models bill ``image_count * pricing["image"]`` on top
    of token I/O. The total includes that image cost."""
    # Pick a model with an image price entry.
    image_models = [
        m for m, p in MODEL_PRICING.items() if "image" in p
    ]
    if not image_models:
        pytest.skip("no image-output models in pricing table")
    model = image_models[0]
    pricing = MODEL_PRICING[model]
    cost = price_call(model, input_tokens=0, output_tokens=0, image_count=3)
    expected_image_cost = 3 * pricing["image"]
    assert cost.image_cost == pytest.approx(expected_image_cost)
    assert cost.total_cost == pytest.approx(expected_image_cost)


def test_price_call_image_count_zero_default():
    """Non-image calls (or image_count=0) produce zero image_cost."""
    cost = price_call("gpt-4o", input_tokens=100, output_tokens=200)
    assert cost.image_cost == 0.0
    cost2 = price_call("gpt-4o", input_tokens=100, output_tokens=200, image_count=0)
    assert cost2.image_cost == 0.0


def test_price_call_image_on_non_image_model_is_free():
    """Even if image_count > 0, a model without an ``image`` price
    contributes 0 — pricing for images on text-only models is undefined."""
    cost = price_call("gpt-4o", input_tokens=0, output_tokens=0, image_count=5)
    assert cost.image_cost == 0.0


def test_pricing_table_shape():
    """Every entry must have input + output keys with non-negative floats.

    Image-output models legitimately invert the input-vs-output text
    relationship (cheap text return, expensive image generation in the
    separate ``image`` field), so the input<=output assertion is skipped
    for entries that declare an ``image`` price.
    """
    for model, p in MODEL_PRICING.items():
        assert "input" in p, f"{model} missing 'input'"
        assert "output" in p, f"{model} missing 'output'"
        assert p["input"] >= 0
        assert p["output"] >= 0
        if "image" in p:
            assert p["image"] >= 0, f"{model} negative 'image' price"
            continue
        # Text-only models: output is virtually always >= input.
        assert p["output"] >= p["input"], f"{model} output cheaper than input"

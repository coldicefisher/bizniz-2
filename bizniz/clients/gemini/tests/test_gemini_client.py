"""Unit tests for GeminiClient — text, JSON schema, and vision."""

import json
from types import SimpleNamespace
import pytest
from unittest.mock import MagicMock, patch, call

from bizniz.clients.gemini.gemini_client import GeminiClient, resolve_gemini_model
from bizniz.clients.gemini.errors import (
    GeminiAuthError,
    GeminiRateLimit,
    GeminiClientError,
)
from bizniz.clients.chatgpt.types.response_format import ResponseFormat


# ── Model resolution ───────────────────────────────────────────────


def test_resolve_gemini_model_known():
    assert resolve_gemini_model("gemini-pro") == "gemini-3.1-pro-preview"
    assert resolve_gemini_model("gemini-flash-lite") == "gemini-2.5-flash-lite"


def test_resolve_gemini_model_passthrough():
    assert resolve_gemini_model("gemini-2.5-pro-preview-05-06") == "gemini-2.5-pro-preview-05-06"


# ── Initialization ────────────────────────────────────────────────


def test_init_requires_api_key():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(GeminiAuthError, match="GEMINI_API_KEY"):
            with patch("bizniz.clients.gemini.gemini_client.genai"):
                GeminiClient(api_key=None)


def test_init_uses_env_key():
    with patch.dict("os.environ", {"GEMINI_API_KEY": "from-env"}):
        with patch("bizniz.clients.gemini.gemini_client.genai") as mock_genai:
            gc = GeminiClient()
            assert gc._api_key == "from-env"


# ── get_text ─────────────────────────────────────────────────────


def test_get_text_basic(gemini_client, mock_genai_client):
    mock_genai_client.models.generate_content.return_value = (
        mock_genai_client._make_response("Hello world")
    )

    text, job_id, msgs = gemini_client.get_text(
        messages=[
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hi"},
        ],
    )

    assert text == "Hello world"
    assert job_id  # non-empty UUID
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] == "Hello world"
    mock_genai_client.models.generate_content.assert_called_once()


def test_get_text_json_schema(gemini_client, mock_genai_client):
    schema = {"name": "test", "schema": {"type": "object", "properties": {"x": {"type": "integer"}}}}
    mock_genai_client.models.generate_content.return_value = (
        mock_genai_client._make_response('{"x": 42}')
    )

    text, _, _ = gemini_client.get_text(
        messages=[{"role": "user", "content": "Give me JSON"}],
        response_format=ResponseFormat.JSON_SCHEMA,
        schema=schema,
    )

    assert json.loads(text) == {"x": 42}
    # Verify the config had json MIME type
    call_args = mock_genai_client.models.generate_content.call_args
    config = call_args.kwargs.get("config") or call_args[1].get("config")
    assert config.response_mime_type == "application/json"


def test_get_text_retries_on_rate_limit(gemini_client, mock_genai_client):
    mock_genai_client.models.generate_content.side_effect = [
        Exception("429 resource_exhausted"),
        mock_genai_client._make_response("ok"),
    ]

    text, _, _ = gemini_client.get_text(
        messages=[{"role": "user", "content": "retry me"}],
    )
    assert text == "ok"
    assert mock_genai_client.models.generate_content.call_count == 2


def test_get_text_raises_auth_error(gemini_client, mock_genai_client):
    mock_genai_client.models.generate_content.side_effect = Exception("403 permission denied")

    with pytest.raises(GeminiAuthError):
        gemini_client.get_text(messages=[{"role": "user", "content": "hi"}])


# ── get_text_with_images (vision) ───���───────────────────────────


def test_vision_with_bytes(gemini_client, mock_genai_client, sample_png_bytes):
    mock_genai_client.models.generate_content.return_value = (
        mock_genai_client._make_response("I see a red pixel")
    )

    text, job_id, msgs = gemini_client.get_text_with_images(
        text_prompt="What is in this image?",
        images=[{"bytes": sample_png_bytes, "mime_type": "image/png"}],
    )

    assert text == "I see a red pixel"
    assert job_id
    # Verify contents had both text and image parts
    call_args = mock_genai_client.models.generate_content.call_args
    contents = call_args.kwargs.get("contents") or call_args[1].get("contents")
    assert len(contents) == 1  # single user message
    assert len(contents[0].parts) == 2  # text + image


def test_vision_with_file_path(gemini_client, mock_genai_client, sample_png_bytes, tmp_path):
    img_path = tmp_path / "test.png"
    img_path.write_bytes(sample_png_bytes)

    mock_genai_client.models.generate_content.return_value = (
        mock_genai_client._make_response("A test image")
    )

    text, _, _ = gemini_client.get_text_with_images(
        text_prompt="Describe this",
        images=[{"path": str(img_path)}],
    )

    assert text == "A test image"


def test_vision_with_system_prompt(gemini_client, mock_genai_client, sample_png_bytes):
    mock_genai_client.models.generate_content.return_value = (
        mock_genai_client._make_response("evaluated")
    )

    gemini_client.get_text_with_images(
        text_prompt="Evaluate this UI",
        images=[{"bytes": sample_png_bytes, "mime_type": "image/png"}],
        system_prompt="You are a UX designer.",
    )

    call_args = mock_genai_client.models.generate_content.call_args
    config = call_args.kwargs.get("config") or call_args[1].get("config")
    assert "UX designer" in config.system_instruction


def test_vision_with_json_schema(gemini_client, mock_genai_client, sample_png_bytes):
    schema = {"name": "eval", "schema": {"type": "object", "properties": {"score": {"type": "integer"}}}}
    mock_genai_client.models.generate_content.return_value = (
        mock_genai_client._make_response('{"score": 7}')
    )

    text, _, _ = gemini_client.get_text_with_images(
        text_prompt="Rate this design",
        images=[{"bytes": sample_png_bytes, "mime_type": "image/png"}],
        schema=schema,
        response_format=ResponseFormat.JSON_SCHEMA,
    )

    assert json.loads(text) == {"score": 7}


def test_vision_multiple_images(gemini_client, mock_genai_client, sample_png_bytes):
    mock_genai_client.models.generate_content.return_value = (
        mock_genai_client._make_response("Two images")
    )

    gemini_client.get_text_with_images(
        text_prompt="Compare these",
        images=[
            {"bytes": sample_png_bytes, "mime_type": "image/png"},
            {"bytes": sample_png_bytes, "mime_type": "image/png"},
        ],
    )

    call_args = mock_genai_client.models.generate_content.call_args
    contents = call_args.kwargs.get("contents") or call_args[1].get("contents")
    assert len(contents[0].parts) == 3  # text + 2 images


def test_vision_bad_image_dict(gemini_client):
    with pytest.raises(ValueError, match="'bytes' or 'path'"):
        gemini_client.get_text_with_images(
            text_prompt="oops",
            images=[{"url": "http://example.com/img.png"}],
        )


# ── Message history ──────────────────────────────────────────────


def test_message_history_accumulates(gemini_client, mock_genai_client):
    mock_genai_client.models.generate_content.return_value = (
        mock_genai_client._make_response("first")
    )
    gemini_client.get_text(messages=[{"role": "user", "content": "q1"}])

    mock_genai_client.models.generate_content.return_value = (
        mock_genai_client._make_response("second")
    )
    gemini_client.get_text(messages=[{"role": "user", "content": "q2"}])

    assert len(gemini_client._message_history) == 4  # 2 user + 2 assistant


def test_clear_message_history(gemini_client, mock_genai_client):
    mock_genai_client.models.generate_content.return_value = (
        mock_genai_client._make_response("hi")
    )
    gemini_client.get_text(messages=[{"role": "user", "content": "q"}])
    assert len(gemini_client._message_history) > 0

    gemini_client.clear_message_history()
    assert len(gemini_client._message_history) == 0


# ── JSON sanitization ────────────────────────────────────────────


def test_extract_first_json():
    assert GeminiClient._extract_first_json('{"a":1}extra') == '{"a":1}'
    assert GeminiClient._extract_first_json('  {"b":2}  ') == '{"b":2}'
    assert GeminiClient._extract_first_json("not json") == "not json"
    assert GeminiClient._extract_first_json("") == ""


# ── _build_contents ──────────────────────────────────────────────


def test_build_contents_merges_consecutive_roles():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"},
    ]
    contents = GeminiClient._build_contents(msgs)
    assert len(contents) == 2  # merged users, then model
    assert contents[0].role == "user"
    assert len(contents[0].parts) == 2
    assert contents[1].role == "model"


def test_build_contents_prepends_user_if_starts_with_model():
    msgs = [{"role": "assistant", "content": "hi"}]
    contents = GeminiClient._build_contents(msgs)
    assert contents[0].role == "user"
    assert contents[1].role == "model"


# ── Image-aware cost tracking ────────────────────────────────────────


def _resp_with_image_parts(n_images: int, n_text: int = 1):
    """Build a Gemini-shaped mock response with ``n_images`` image
    parts and ``n_text`` text parts in candidates[0].content.parts."""
    parts = []
    for _ in range(n_text):
        parts.append(SimpleNamespace(text="hello", inline_data=None))
    for _ in range(n_images):
        parts.append(SimpleNamespace(
            text=None,
            inline_data=SimpleNamespace(
                mime_type="image/png", data=b"\x89PNG",
            ),
        ))
    cand = SimpleNamespace(content=SimpleNamespace(parts=parts))
    resp = MagicMock()
    resp.text = "hello"
    resp.candidates = [cand]
    resp.usage_metadata = SimpleNamespace(
        prompt_token_count=10, candidates_token_count=5,
    )
    return resp


class TestCountImageParts:
    def test_zero_when_no_candidates(self):
        resp = MagicMock()
        resp.candidates = None
        assert GeminiClient._count_image_parts(resp) == 0

    def test_zero_when_only_text_parts(self):
        resp = _resp_with_image_parts(n_images=0, n_text=2)
        assert GeminiClient._count_image_parts(resp) == 0

    def test_counts_image_parts(self):
        resp = _resp_with_image_parts(n_images=3, n_text=1)
        assert GeminiClient._count_image_parts(resp) == 3

    def test_ignores_non_image_inline_data(self):
        # Some other inline_data type, e.g. application/pdf — should not count.
        cand = SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(text=None, inline_data=SimpleNamespace(
                mime_type="application/pdf", data=b"...",
            )),
            SimpleNamespace(text="ok", inline_data=None),
        ]))
        resp = MagicMock()
        resp.candidates = [cand]
        assert GeminiClient._count_image_parts(resp) == 0

    def test_counts_across_multiple_candidates(self):
        cand1 = SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(text=None, inline_data=SimpleNamespace(
                mime_type="image/jpeg", data=b"."
            )),
        ]))
        cand2 = SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(text=None, inline_data=SimpleNamespace(
                mime_type="image/png", data=b"."
            )),
        ]))
        resp = MagicMock()
        resp.candidates = [cand1, cand2]
        assert GeminiClient._count_image_parts(resp) == 2

    def test_swallows_exceptions(self):
        # Bogus shape — must return 0, never raise.
        resp = MagicMock()
        resp.candidates = "not iterable in the way we expect"
        # Iterating a string yields chars; getattr(char, "content")
        # raises AttributeError. Helper catches.
        assert GeminiClient._count_image_parts(resp) == 0


def test_get_text_records_image_count_in_tracker(gemini_client, mock_genai_client):
    """Image-output responses populate ``image_count`` on the cost
    record. Verifies the helper feeds into get_text's tracker call."""
    from bizniz.cost import get_tracker
    tracker = get_tracker()
    tracker.reset()

    mock_genai_client.models.generate_content.return_value = (
        _resp_with_image_parts(n_images=2)
    )
    gemini_client.get_text(
        messages=[{"role": "user", "content": "make 2 images"}],
    )

    # Verify the recorded call carries image_count=2.
    records = tracker.records()
    assert len(records) >= 1
    rec = records[-1]
    assert rec.image_count == 2

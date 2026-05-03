"""Functional tests — hit real Gemini API. Run with: pytest -m functional"""

import json
import os
import struct
import zlib
import pytest

from bizniz.clients.gemini.gemini_client import GeminiClient
from bizniz.clients.chatgpt.types.response_format import ResponseFormat


pytestmark = pytest.mark.functional


@pytest.fixture
def client():
    return GeminiClient(model_name="gemini-flash-lite")


@pytest.fixture
def sample_png_bytes():
    """Minimal valid 1x1 red PNG."""
    def _chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = zlib.compress(b"\x00\xff\x00\x00")
    idat = _chunk(b"IDAT", raw)
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def test_get_text_real(client):
    text, job_id, msgs = client.get_text(
        messages=[
            {"role": "system", "content": "Answer in one word."},
            {"role": "user", "content": "What color is the sky?"},
        ],
    )
    assert len(text) > 0
    assert job_id
    assert "blue" in text.lower()


def test_get_text_json_schema_real(client):
    schema = {
        "name": "color",
        "schema": {
            "type": "object",
            "properties": {
                "color": {"type": "string"},
            },
            "required": ["color"],
        },
    }
    text, _, _ = client.get_text(
        messages=[{"role": "user", "content": "What color is grass? Respond as JSON."}],
        response_format=ResponseFormat.JSON_SCHEMA,
        schema=schema,
    )
    parsed = json.loads(text)
    assert "color" in parsed
    assert "green" in parsed["color"].lower()


def test_vision_real(client, sample_png_bytes):
    text, job_id, msgs = client.get_text_with_images(
        text_prompt="Describe what you see in this image in one sentence.",
        images=[{"bytes": sample_png_bytes, "mime_type": "image/png"}],
    )
    assert len(text) > 0
    assert job_id
    # It's a 1x1 red pixel — should mention red, pixel, small, or image
    lower = text.lower()
    assert any(w in lower for w in ("red", "pixel", "image", "small", "color", "single"))


def test_vision_json_schema_real(client, sample_png_bytes):
    schema = {
        "name": "image_eval",
        "schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "dominant_color": {"type": "string"},
            },
            "required": ["description", "dominant_color"],
        },
    }
    text, _, _ = client.get_text_with_images(
        text_prompt="Analyze this image and respond as JSON.",
        images=[{"bytes": sample_png_bytes, "mime_type": "image/png"}],
        schema=schema,
        response_format=ResponseFormat.JSON_SCHEMA,
    )
    parsed = json.loads(text)
    assert "description" in parsed
    assert "dominant_color" in parsed

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_genai_client():
    """A mock google.genai.Client that returns canned responses."""
    client = MagicMock()

    def _make_response(text="Hello", input_tokens=10, output_tokens=5):
        resp = MagicMock()
        resp.text = text
        resp.usage_metadata = SimpleNamespace(
            prompt_token_count=input_tokens,
            candidates_token_count=output_tokens,
        )
        return resp

    client._make_response = _make_response
    client.models.generate_content.return_value = _make_response()
    return client


@pytest.fixture
def gemini_client(mock_genai_client):
    """A GeminiClient with a mocked genai.Client underneath."""
    with patch("bizniz.clients.gemini.gemini_client.genai") as mock_genai:
        mock_genai.Client.return_value = mock_genai_client
        from bizniz.clients.gemini.gemini_client import GeminiClient
        gc = GeminiClient(api_key="test-key", model_name="gemini-pro")
        gc._client = mock_genai_client
        return gc


@pytest.fixture
def sample_png_bytes():
    """Minimal valid 1x1 red PNG for vision tests."""
    import struct, zlib
    def _chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = zlib.compress(b"\x00\xff\x00\x00")  # filter=none, R=255 G=0 B=0
    idat = _chunk(b"IDAT", raw)
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend

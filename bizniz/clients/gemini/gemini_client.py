"""
GeminiClient

Implements BaseAIClient using the Google GenAI SDK.
Supports Gemini models (gemini-2.5-flash-lite, gemini-2.5-flash, gemini-2.5-pro)
with message history management and structured JSON output.
"""

import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Callable, Union

from google import genai
from google.genai import types

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message, MessageList
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.clients.gemini.errors import (
    GeminiClientError,
    GeminiRateLimit,
    GeminiInsufficientFunds,
    GeminiAuthError,
    GeminiInvalidRequest,
    GeminiContextLengthExceeded,
)


GEMINI_MODELS = {
    "gemini-flash-lite": "gemini-2.5-flash-lite",
    "gemini-flash": "gemini-3.1-flash-lite-preview",
    "gemini-flash-top": "gemini-3-flash-preview",
    "gemini-pro": "gemini-3.1-pro-preview",
}


def resolve_gemini_model(model_name: str) -> str:
    """Resolve a short model name to a full Gemini model ID."""
    return GEMINI_MODELS.get(model_name, model_name)


class GeminiClient(BaseAIClient):
    """
    AI client using the Google Gemini API.

    Supports structured JSON output via system prompts. For JSON_SCHEMA
    mode, the schema is included in the system prompt and the model is
    instructed to respond with valid JSON only.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "gemini-2.5-flash",
        max_tokens: int = 32_000,
        on_message_callback: Optional[Callable[[Message], None]] = None,
    ):
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self._api_key:
            raise GeminiAuthError("GEMINI_API_KEY must be set or passed directly.")

        self._model_name = resolve_gemini_model(model_name)
        self.max_tokens = max_tokens
        self.on_message_callback = on_message_callback
        self._message_history: List[Dict[str, str]] = []
        self._client = genai.Client(api_key=self._api_key)

    @property
    def ai_agent(self) -> Any:
        return self._client

    def set_model(self, model_name: str) -> None:
        self._model_name = resolve_gemini_model(model_name)

    def clear_message_history(self):
        self._message_history = []

    def get_text(
        self,
        messages,
        message_history: MessageList = None,
        message_history_filepath: str = None,
        use_message_history: bool = True,
        message_history_limit: int = 10,
        schema: dict = None,
        response_format: ResponseFormat = ResponseFormat.TEXT,
        max_tokens: Optional[int] = None,
        job_description: Optional[str] = None,
        temperature: float = 0.0,
        cached_content_name: Optional[str] = None,
        cache_prefix_count: int = 0,
        **kwargs,
    ) -> Tuple[str, str, List]:
        """
        Call the Gemini API and return (text, job_id, output_messages).

        For JSON_SCHEMA mode, the schema is embedded in the system instruction
        and the model is instructed to respond with valid JSON only.
        """
        normalized = self._normalize_messages(messages)

        # Extract system message
        system_content = ""
        user_messages = []
        for msg in normalized:
            if msg["role"] == "system":
                system_content += msg["content"] + "\n"
            else:
                user_messages.append(msg)

        # Handle JSON schema mode
        if response_format == ResponseFormat.JSON_SCHEMA and schema:
            schema_text = self._format_schema_prompt(schema)
            system_content += f"\n{schema_text}"
        elif response_format == ResponseFormat.JSON:
            system_content += (
                "\nYou must respond with valid JSON only. "
                "Do not include any text before or after the JSON."
            )

        # Build message list with history
        if use_message_history:
            history = self._message_history[-message_history_limit:] if message_history_limit else self._message_history
            api_messages = history + user_messages
        else:
            api_messages = user_messages

        # PROMPT CACHING (lever A from cost analysis): when the caller
        # passes ``cached_content_name``, drop the first
        # ``cache_prefix_count`` entries of api_messages — those are
        # already in the cache. The system instruction is also already
        # in the cache, so omit it from the per-call config.
        omit_system_instruction = False
        if cached_content_name and cache_prefix_count > 0:
            api_messages = api_messages[cache_prefix_count:]
            omit_system_instruction = True
            # Gemini requires at least one entry in contents even when
            # cached_content is set. On the first tool-loop iteration
            # after cache creation, api_messages can legitimately be
            # empty (the cache contains everything we've sent so far).
            # Append a tiny nudge so the API has something to generate
            # against.
            if not api_messages:
                api_messages = [{"role": "user", "content": "Continue."}]

        # Convert to Gemini contents format
        contents = self._build_contents(api_messages)

        # Build config
        config = types.GenerateContentConfig(
            system_instruction=(
                None if omit_system_instruction
                else (system_content.strip() if system_content.strip() else None)
            ),
            max_output_tokens=max_tokens or self.max_tokens,
            temperature=temperature,
        )
        if cached_content_name:
            config.cached_content = cached_content_name

        # If JSON mode, set response MIME type
        if response_format in (ResponseFormat.JSON, ResponseFormat.JSON_SCHEMA):
            config.response_mime_type = "application/json"

        # Call API with retry
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                _t_start = time.time()
                response = self._client.models.generate_content(
                    model=self._model_name,
                    contents=contents,
                    config=config,
                )
                _duration_ms = int((time.time() - _t_start) * 1000)

                response_text = response.text or ""

                # Record usage to the global cost tracker.
                try:
                    from bizniz.cost import get_tracker
                    usage = getattr(response, "usage_metadata", None)
                    in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
                    out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
                    cached_tok = int(
                        getattr(usage, "cached_content_token_count", 0) or 0
                    )
                    img_count = self._count_image_parts(response)
                    if in_tok or out_tok or img_count:
                        get_tracker().record(
                            agent=getattr(self, "_caller_agent", "unknown"),
                            model=self._model_name,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            duration_ms=_duration_ms,
                            image_count=img_count,
                            cached_input_tokens=cached_tok,
                        )
                except Exception:
                    # Cost tracking is best-effort; never break a real call.
                    pass

                # Sanitize JSON responses from Gemini quirks:
                # 1. Raw control chars inside string values (newlines, tabs)
                # 2. Extra data after the JSON object (trailing objects/text)
                if response_format in (ResponseFormat.JSON, ResponseFormat.JSON_SCHEMA):
                    response_text = self._sanitize_json(response_text)
                    response_text = self._extract_first_json(response_text)

                job_id = str(uuid.uuid4())

                # Update message history
                filtered = [m for m in user_messages if m["role"] != "system"]
                self._message_history.extend(filtered)
                output_message = {"role": "assistant", "content": response_text}
                self._message_history.append(output_message)

                if self.on_message_callback:
                    self.on_message_callback(Message(
                        role="assistant", content=response_text
                    ))

                return response_text, job_id, [output_message]

            except Exception as e:
                error_msg = str(e).lower()

                # Authentication errors
                if any(phrase in error_msg for phrase in [
                    "api_key_invalid", "api key not valid", "authentication",
                    "permission denied", "403",
                ]):
                    raise GeminiAuthError(str(e))

                # Insufficient funds / quota
                if any(phrase in error_msg for phrase in [
                    "quota", "billing", "resource_exhausted",
                    "insufficient", "429",
                ]):
                    # Distinguish rate limit (retriable) vs quota exhausted (terminal)
                    if any(phrase in error_msg for phrase in [
                        "billing", "insufficient", "quota exceeded",
                    ]):
                        raise GeminiInsufficientFunds(str(e))
                    if attempt < max_retries:
                        wait = min(5.0 * attempt, 30.0)
                        time.sleep(wait)
                        continue
                    raise GeminiRateLimit(str(e))

                # Context length
                if any(phrase in error_msg for phrase in [
                    "context_length", "token limit", "too long",
                    "max_tokens", "content too large",
                    "request payload size exceeds",
                ]):
                    raise GeminiContextLengthExceeded(str(e))

                # Invalid request
                if any(phrase in error_msg for phrase in [
                    "invalid", "bad request", "400",
                ]):
                    raise GeminiInvalidRequest(str(e))

                raise GeminiClientError(f"Gemini API error: {e}")

    def get_text_with_images(
        self,
        text_prompt: str,
        images: List[Dict[str, Any]],
        system_prompt: str = "",
        schema: dict = None,
        response_format: ResponseFormat = ResponseFormat.TEXT,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        job_description: Optional[str] = None,
    ) -> Tuple[str, str, List]:
        """Call the Gemini API with text + images (vision).

        Parameters
        ----------
        text_prompt:
            The text portion of the user message.
        images:
            List of image dicts. Each must have either:
              - ``bytes`` (raw bytes) + ``mime_type`` (e.g. "image/png")
              - ``path`` (str or Path to an image file)
        system_prompt:
            Optional system instruction prepended to the request.
        schema / response_format:
            Same semantics as ``get_text()``.

        Returns
        -------
        Same (text, job_id, output_messages) tuple as ``get_text()``.
        """
        # Build multimodal parts: text + image parts
        parts = [types.Part.from_text(text=text_prompt)]
        for img in images:
            parts.append(self._image_to_part(img))

        contents = [types.Content(role="user", parts=parts)]

        # System instruction
        system_content = system_prompt or ""
        if response_format == ResponseFormat.JSON_SCHEMA and schema:
            system_content += f"\n{self._format_schema_prompt(schema)}"
        elif response_format == ResponseFormat.JSON:
            system_content += (
                "\nYou must respond with valid JSON only. "
                "Do not include any text before or after the JSON."
            )

        config = types.GenerateContentConfig(
            system_instruction=system_content.strip() if system_content.strip() else None,
            max_output_tokens=max_tokens or self.max_tokens,
            temperature=temperature,
        )
        if response_format in (ResponseFormat.JSON, ResponseFormat.JSON_SCHEMA):
            config.response_mime_type = "application/json"

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                _t_start = time.time()
                response = self._client.models.generate_content(
                    model=self._model_name,
                    contents=contents,
                    config=config,
                )
                _duration_ms = int((time.time() - _t_start) * 1000)
                response_text = response.text or ""

                # Cost tracking
                try:
                    from bizniz.cost import get_tracker
                    usage = getattr(response, "usage_metadata", None)
                    in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
                    out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
                    img_count = self._count_image_parts(response)
                    if in_tok or out_tok or img_count:
                        get_tracker().record(
                            agent=getattr(self, "_caller_agent", "unknown"),
                            model=self._model_name,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            duration_ms=_duration_ms,
                            image_count=img_count,
                        )
                except Exception:
                    pass

                if response_format in (ResponseFormat.JSON, ResponseFormat.JSON_SCHEMA):
                    response_text = self._sanitize_json(response_text)
                    response_text = self._extract_first_json(response_text)

                job_id = str(uuid.uuid4())
                output_message = {"role": "assistant", "content": response_text}
                self._message_history.append(output_message)

                return response_text, job_id, [output_message]

            except Exception as e:
                error_msg = str(e).lower()
                if any(p in error_msg for p in ["api_key_invalid", "permission denied", "403"]):
                    raise GeminiAuthError(str(e))
                if any(p in error_msg for p in ["quota", "billing", "resource_exhausted", "429"]):
                    if attempt < max_retries:
                        time.sleep(min(5.0 * attempt, 30.0))
                        continue
                    raise GeminiRateLimit(str(e))
                if any(p in error_msg for p in ["context_length", "too long", "content too large"]):
                    raise GeminiContextLengthExceeded(str(e))
                raise GeminiClientError(f"Gemini vision API error: {e}")

    @staticmethod
    def _image_to_part(img: Dict[str, Any]) -> types.Part:
        """Convert an image dict to a Gemini Part.

        Accepts either ``{"bytes": b"...", "mime_type": "image/png"}``
        or ``{"path": "/path/to/file.png"}``.
        """
        if "bytes" in img:
            mime = img.get("mime_type", "image/png")
            return types.Part.from_bytes(data=img["bytes"], mime_type=mime)
        elif "path" in img:
            p = Path(img["path"])
            data = p.read_bytes()
            mime = img.get("mime_type") or mimetypes.guess_type(str(p))[0] or "image/png"
            return types.Part.from_bytes(data=data, mime_type=mime)
        else:
            raise ValueError(f"Image dict must have 'bytes' or 'path' key, got: {list(img.keys())}")

    @staticmethod
    def _format_schema_prompt(schema: dict) -> str:
        """Convert a JSON schema into a system prompt instruction."""
        if "schema" in schema:
            schema_body = schema["schema"]
            schema_name = schema.get("name", "response")
        else:
            schema_body = schema
            schema_name = "response"

        return (
            f"You must respond with valid JSON matching this schema (name: {schema_name}):\n"
            f"```json\n{json.dumps(schema_body, indent=2)}\n```\n"
            f"Respond with ONLY the JSON object. No markdown, no explanation, no text "
            f"before or after the JSON."
        )

    @staticmethod
    def _normalize_messages(messages) -> List[Dict[str, str]]:
        """Normalize messages to list of dicts."""
        result = []
        for msg in messages:
            if isinstance(msg, dict):
                result.append({"role": msg["role"], "content": msg["content"]})
            elif isinstance(msg, Message):
                result.append({"role": msg.role, "content": msg.content})
            else:
                result.append({"role": str(getattr(msg, "role", "user")),
                               "content": str(getattr(msg, "content", str(msg)))})
        return result

    @staticmethod
    def _build_contents(messages: List[Dict[str, str]]) -> list:
        """Convert messages to Gemini contents format.

        Gemini uses 'user' and 'model' roles (not 'assistant').
        """
        contents = []
        for msg in messages:
            role = msg["role"]
            if role == "assistant":
                role = "model"
            elif role not in ("user", "model"):
                role = "user"
            contents.append(types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg["content"])],
            ))

        # Gemini requires first message to be from user
        if contents and contents[0].role != "user":
            contents.insert(0, types.Content(
                role="user",
                parts=[types.Part.from_text(text="Begin.")],
            ))

        # Merge consecutive same-role messages (Gemini requires alternation)
        merged = []
        for content in contents:
            if merged and merged[-1].role == content.role:
                merged[-1].parts.extend(content.parts)
            else:
                merged.append(content)

        return merged

    def try_create_cache(self, messages) -> Optional[str]:
        """Create a Gemini cached_content prefix for ``messages`` (typically
        the system + initial user message of a tool-loop run).

        Returns the cache resource name (``"cachedContents/abc123"``) or
        None on any failure. Subsequent ``get_text`` calls reuse the
        cache via ``cached_content_name=...`` and pass
        ``cache_prefix_count=len(messages)`` so the cached prefix is
        stripped from the per-call payload.

        TTL is fixed at 1 hour — the Engineer's tool-loop typically
        finishes in 5-10 minutes, so the cache stays warm for the
        whole run.
        """
        try:
            normalized = self._normalize_messages(messages)
            system_content = ""
            user_msgs = []
            for m in normalized:
                if m["role"] == "system":
                    system_content += m["content"] + "\n"
                else:
                    user_msgs.append(m)
            contents = self._build_contents(user_msgs)
            cache = self._client.caches.create(
                model=f"models/{self._model_name}",
                config=types.CreateCachedContentConfig(
                    system_instruction=(
                        system_content.strip() if system_content.strip() else None
                    ),
                    contents=contents,
                    ttl="3600s",
                ),
            )
            return cache.name
        except Exception as e:
            # Caching failures should never break the tool loop.
            # Common reasons: content too small, model doesn't support
            # caching, transient API error.
            try:
                from bizniz.cost import get_tracker
                # No-op record; tracker doesn't have a "cache_failed"
                # category yet but useful breadcrumb in logs.
            except Exception:
                pass
            return None

    @staticmethod
    def _count_image_parts(response) -> int:
        """Count image parts in a Gemini response.

        Walks ``response.candidates[*].content.parts[*]`` and counts any
        part with ``inline_data.mime_type`` starting with ``image/``.
        Returns 0 on any error (cost tracking is best-effort and the
        SDK shape varies across versions).
        """
        try:
            count = 0
            candidates = getattr(response, "candidates", None) or []
            for cand in candidates:
                content = getattr(cand, "content", None)
                if content is None:
                    continue
                parts = getattr(content, "parts", None) or []
                for part in parts:
                    inline = getattr(part, "inline_data", None)
                    if inline is None:
                        continue
                    mime = getattr(inline, "mime_type", "") or ""
                    if str(mime).lower().startswith("image/"):
                        count += 1
            return count
        except Exception:
            return 0

    @staticmethod
    def _sanitize_json(text: str) -> str:
        """Fix raw control chars and invalid backslash escapes inside JSON
        string values. Delegates to the shared bizniz.utils.json helper so
        every JSON-parsing path in the system stays consistent.
        """
        from bizniz.utils.json import fix_string_escapes
        return fix_string_escapes(text)

    @staticmethod
    def _extract_first_json(text: str) -> str:
        """Extract the first complete JSON object from the response.

        Gemini sometimes returns extra data after the main JSON object
        (e.g., a second object, trailing explanation text). This uses
        json.JSONDecoder.raw_decode to parse only the first object.
        """
        text = text.strip()
        if not text:
            return text
        try:
            decoder = json.JSONDecoder()
            _, end_idx = decoder.raw_decode(text)
            return text[:end_idx]
        except json.JSONDecodeError:
            return text

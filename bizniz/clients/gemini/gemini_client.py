"""
GeminiClient

Implements BaseAIClient using the Google GenAI SDK.
Supports Gemini models (gemini-2.5-flash-lite, gemini-2.5-flash, gemini-2.5-pro)
with message history management and structured JSON output.
"""

import json
import os
import re
import time
import uuid
from typing import Optional, List, Dict, Any, Tuple, Callable

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
        max_tokens: int = 16_000,
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

        # Convert to Gemini contents format
        contents = self._build_contents(api_messages)

        # Build config
        config = types.GenerateContentConfig(
            system_instruction=system_content.strip() if system_content.strip() else None,
            max_output_tokens=max_tokens or self.max_tokens,
            temperature=temperature,
        )

        # If JSON mode, set response MIME type
        if response_format in (ResponseFormat.JSON, ResponseFormat.JSON_SCHEMA):
            config.response_mime_type = "application/json"

        # Call API with retry
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._model_name,
                    contents=contents,
                    config=config,
                )

                response_text = response.text or ""

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

    @staticmethod
    def _sanitize_json(text: str) -> str:
        """Fix invalid control characters inside JSON string values.

        Gemini sometimes embeds raw newlines, tabs, and other control chars
        inside JSON string values instead of proper escape sequences.
        This walks the string, and when inside a JSON string literal,
        replaces raw control chars with their escaped forms.
        """
        result = []
        in_string = False
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == '\\' and in_string:
                # Escaped character — pass through both chars
                result.append(ch)
                if i + 1 < len(text):
                    i += 1
                    result.append(text[i])
                i += 1
                continue
            if ch == '"':
                in_string = not in_string
                result.append(ch)
                i += 1
                continue
            if in_string and ord(ch) < 0x20:
                # Raw control character inside a string — escape it
                escape_map = {
                    '\n': '\\n',
                    '\r': '\\r',
                    '\t': '\\t',
                    '\x08': '\\b',
                    '\x0c': '\\f',
                }
                result.append(escape_map.get(ch, f'\\u{ord(ch):04x}'))
            else:
                result.append(ch)
            i += 1
        return ''.join(result)

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

"""
ClaudeClient

Implements BaseAIClient using the Anthropic SDK.
Supports Claude models (claude-sonnet-4-20250514, claude-opus-4-20250514, etc.)
with message history management and structured JSON output.
"""

import json
import os
import re
import time
import uuid
from typing import Optional, List, Dict, Any, Tuple, Callable

import anthropic

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message, MessageList
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.clients.claude.errors import (
    ClaudeClientError,
    ClaudeRateLimit,
    ClaudeInsufficientFunds,
    ClaudeAuthError,
    ClaudeInvalidRequest,
)


CLAUDE_MODELS = {
    "claude-sonnet": "claude-sonnet-4-20250514",
    "claude-opus": "claude-opus-4-20250514",
    "claude-haiku": "claude-haiku-4-5-20251001",
}


def resolve_claude_model(model_name: str) -> str:
    """Resolve a short model name to a full Claude model ID."""
    return CLAUDE_MODELS.get(model_name, model_name)


class ClaudeClient(BaseAIClient):
    """
    AI client using the Anthropic Claude API.

    Supports structured JSON output via system prompts and prefilled
    assistant responses. For JSON_SCHEMA mode, the schema is included
    in the system prompt and the response is parsed as JSON.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "claude-sonnet-4-20250514",
        max_tokens: int = 16_000,
        on_message_callback: Optional[Callable[[Message], None]] = None,
    ):
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise ClaudeAuthError("ANTHROPIC_API_KEY must be set or passed directly.")

        self._model_name = resolve_claude_model(model_name)
        self.max_tokens = max_tokens
        self.on_message_callback = on_message_callback
        self._message_history: List[Dict[str, str]] = []
        self._client = anthropic.Anthropic(api_key=self._api_key)

    @property
    def ai_agent(self) -> Any:
        return self._client

    def set_model(self, model_name: str) -> None:
        self._model_name = resolve_claude_model(model_name)

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
        Call the Claude API and return (text, job_id, output_messages).

        For JSON_SCHEMA mode, the schema is embedded in the system prompt
        and the model is instructed to respond with valid JSON only.
        """
        # Normalize messages to dicts
        normalized = self._normalize_messages(messages)

        # Extract system message if present
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

        # Ensure messages alternate user/assistant (Claude requirement)
        api_messages = self._ensure_alternating(api_messages)

        # Call API with retry
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response_text = ""
                _t_start = time.time()
                _final_message = None
                with self._client.messages.stream(
                    model=self._model_name,
                    max_tokens=max_tokens or self.max_tokens,
                    system=system_content.strip() if system_content.strip() else None,
                    messages=api_messages,
                    temperature=temperature,
                ) as stream:
                    for text in stream.text_stream:
                        response_text += text
                    try:
                        _final_message = stream.get_final_message()
                    except Exception:
                        _final_message = None
                _duration_ms = int((time.time() - _t_start) * 1000)

                # Record cost. Anthropic exposes usage on the final message.
                try:
                    from bizniz.cost import get_tracker
                    if _final_message is not None and getattr(_final_message, "usage", None):
                        usage = _final_message.usage
                        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
                        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
                        if in_tok or out_tok:
                            get_tracker().record(
                                agent=getattr(self, "_caller_agent", "unknown"),
                                model=self._model_name,
                                input_tokens=in_tok,
                                output_tokens=out_tok,
                                duration_ms=_duration_ms,
                            )
                except Exception:
                    pass

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

            except anthropic.AuthenticationError as e:
                raise ClaudeAuthError(str(e))
            except anthropic.RateLimitError as e:
                error_msg = str(e).lower()
                if any(phrase in error_msg for phrase in [
                    "insufficient", "quota", "billing", "credit",
                ]):
                    raise ClaudeInsufficientFunds(str(e))
                if attempt < max_retries:
                    wait = min(5.0 * attempt, 30.0)
                    time.sleep(wait)
                    continue
                raise ClaudeRateLimit(str(e))
            except anthropic.BadRequestError as e:
                from bizniz.clients.claude.errors import ClaudeContextLengthExceeded
                error_msg = str(e).lower()
                if "context_length_exceeded" in error_msg or "context window" in error_msg or "too many tokens" in error_msg:
                    raise ClaudeContextLengthExceeded(str(e))
                raise ClaudeInvalidRequest(str(e))
            except Exception as e:
                raise ClaudeClientError(f"Claude API error: {e}")

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
    def _ensure_alternating(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Ensure messages alternate between user and assistant.

        Claude requires strict alternation. If two consecutive messages
        have the same role, merge them.
        """
        if not messages:
            return messages

        result = [messages[0]]
        for msg in messages[1:]:
            if msg["role"] == result[-1]["role"]:
                result[-1]["content"] += "\n\n" + msg["content"]
            else:
                result.append(msg)

        # Claude requires the first message to be from user
        if result and result[0]["role"] != "user":
            result.insert(0, {"role": "user", "content": "Begin."})

        return result

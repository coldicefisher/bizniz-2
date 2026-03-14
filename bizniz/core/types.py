"""
Shared types used across the bizniz pipeline.

These are provider-agnostic types used by all AI clients and agents.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

import json

from pydantic import BaseModel


# --- Roles ---

class Role(Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


# --- Response Format ---

class ResponseFormat(Enum):
    JSON = "json_object"
    TEXT = "text"
    JSON_SCHEMA = "json_schema"


def parse_response_format(response_format: Optional[ResponseFormat] = None, schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # Default to JSON response format if none provided
    if response_format is None:
        return {"type": "json_object"}

    if response_format is not None and isinstance(response_format, (str, ResponseFormat)):
        match response_format:
            case ResponseFormat.JSON | "json_object":
                return {"type": "json_object"}
            case ResponseFormat.TEXT | "text":
                return {"type": "text"}
            case ResponseFormat.JSON_SCHEMA | "json_schema":
                if schema is None or not isinstance(schema, dict):
                    raise ValueError("Schema must be provided for JSON_SCHEMA response format and must be a dict.")
                # Unwrap schema wrappers: if the schema has a "schema" key,
                # it's a wrapper like {name, strict, schema: {type: "object", ...}}
                if "schema" in schema:
                    schema_name = schema.get("name", "response")
                    schema_body = schema["schema"]
                    strict = schema.get("strict", True)
                else:
                    schema_name = "response"
                    schema_body = schema
                    strict = True
                return {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema_body,
                    "strict": strict,
                }
    else:
        return {"type": "json_object"}


# --- Messages ---

@dataclass
class Message:
    role: Role
    content: str

    def to_dict(self) -> Dict[str, Any]:
        _role = self.role.value if isinstance(self.role, Role) else str(self.role)
        return {"role": _role, "content": self.content}


@dataclass
class MessageList:
    messages: List[Message]
    input_tokens: float = 0.0
    output_tokens: float = 0.0

    def __iter__(self):
        return iter(self.messages)

    def __len__(self):
        return len(self.messages)

    def __getitem__(self, index):
        return self.messages[index]

    def to_dict(self) -> List[Dict[str, str]]:
        if type(self.messages) is list and type(self.messages[0]) is dict:
            return self.messages  # Already in dict form

        if type(self.messages) is list and type(self.messages[0]) is Message:
            return [m.to_dict() for m in self.messages]

        return [{"error": "Unknown message type"}]

    def __str__(self):
        s = "Input Tokens: {}, Output Tokens: {}\n".format(self.input_tokens, self.output_tokens)

        for m in self.messages:
            role = None
            if isinstance(m.role, Role):
                role = m.role.value
            else:
                role = str(m.role)

            s += f"{role}: {m.content}\n"
        return s

    def __repr__(self):
        return self.__str__()

    def add(self, role: Role, content: str) -> None:
        self.messages.append(Message(role, content))


def normalize_messages(messages: Union[List[Dict[str, Any]], List[Message], MessageList]) -> List[Dict[str, Any]]:

        if messages is None:
            return None

        if isinstance(messages, MessageList):
            return messages.to_dict()

        if isinstance(messages, list):
            normalized = []
            for m in messages:
                if isinstance(m, Message):
                    normalized.append(m.to_dict())
                else:
                    normalized.append(m)
            return normalized

        return messages


# --- File Changes ---

class FileChange(BaseModel):
    """A single file create/modify/delete in a multi-file code generation."""
    filepath: str
    code: str
    action: Literal["create", "modify", "delete"]

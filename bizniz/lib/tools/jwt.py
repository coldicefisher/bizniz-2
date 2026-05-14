"""JWT decode tool factory for v2 tool-loop agents.

Pure-Python utility — no docker, no network. Decodes header + payload
without verifying the signature. Useful for any agent that grabs a
token and wants to inspect ``iss``, ``aud``, ``roles``, ``exp``, etc.

The signature is intentionally NOT verified; the agent doesn't have
the JWKS in-process and the tool's job is to expose the claims, not
to authenticate. If the agent needs to verify, it should call
``smoke_login`` against the live stack and observe whether protected
endpoints accept the token.
"""
from __future__ import annotations

import base64
import json
from typing import Callable, Dict


ToolHandler = Callable[[Dict], str]


def make_decode_jwt() -> ToolHandler:
    """Decode a JWT's header + payload without verifying.

    Action fields:
      - ``token``: the JWT string (with or without the "Bearer " prefix)
    """
    def handler(action: Dict) -> str:
        token = (action.get("token") or "").strip()
        if not token:
            return "ERROR: decode_jwt requires a non-empty 'token'."
        if token.lower().startswith("bearer "):
            token = token.split(None, 1)[1]
        parts = token.split(".")
        if len(parts) != 3:
            return f"ERROR: not a JWT (expected 3 parts, got {len(parts)})."
        try:
            def _decode(seg: str) -> dict:
                pad = seg + "=" * (-len(seg) % 4)
                return json.loads(base64.urlsafe_b64decode(pad.encode()))
            header = _decode(parts[0])
            payload = _decode(parts[1])
        except Exception as e:
            return f"ERROR: could not decode: {e}"
        return (
            "=== JWT (signature NOT verified) ===\n"
            f"Header:\n{json.dumps(header, indent=2)}\n\n"
            f"Payload:\n{json.dumps(payload, indent=2)}"
        )
    return handler


def build_jwt_handlers() -> Dict[str, ToolHandler]:
    return {"decode_jwt": make_decode_jwt()}

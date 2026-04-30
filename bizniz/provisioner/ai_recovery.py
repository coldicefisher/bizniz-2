"""AI-assisted recovery for failed Docker image builds.

When ``build_image()`` raises during the per-action build phase, the
Provisioner can opt into asking an AI to read the Dockerfile + build
error and return a patched Dockerfile. We retry up to N times.

Structural guarantee: the AI's schema is **Dockerfile-only**. It cannot
emit compose changes, ports, depends_on, or environment variables —
those would invalidate the architect's plan.

Opt-in only — `Provisioner(ai_recovery_enabled=True, ai_client_factory=...)`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel

from bizniz.core.client import BaseAIClient
from bizniz.core.types import ResponseFormat


_LOG = logging.getLogger(__name__)


AIRecoverySchema = {
    "name": "ai_recovery_dockerfile",
    "strict": True,
    "schema": {
        "type": "object",
        "required": ["dockerfile", "explanation"],
        "additionalProperties": False,
        "properties": {
            "dockerfile": {
                "type": "string",
                "description": (
                    "Patched Dockerfile content — full file, NOT a diff. "
                    "Do NOT add EXPOSE, ports, networks, or volumes. "
                    "Fix the build error only."
                ),
            },
            "explanation": {
                "type": "string",
                "description": "Brief: what was wrong and how the fix addresses it.",
            },
        },
    },
}


_SYSTEM_PROMPT = """You are a Docker build doctor. Given a Dockerfile that failed to build and the build error log, return a patched Dockerfile that fixes the error.

Constraints:
  - Output the full Dockerfile, not a diff.
  - Do NOT add EXPOSE, port mappings, networks, or volumes — those are managed elsewhere.
  - Do NOT change the service's purpose. If the original installs FastAPI, the fix should still install FastAPI.
  - Prefer the smallest change that fixes the build. Don't refactor unrelated parts.
  - If the error suggests a missing system package, add the apt-get install line.
  - If the error is a Python/Node dependency conflict, pin or unpin versions as needed.
  - If the base image is wrong (e.g. python:3.10 missing a needed lib), upgrade the base."""


class AIRecoveryResponse(BaseModel):
    dockerfile: str
    explanation: str = ""


def call_ai_recovery(
    client: BaseAIClient,
    dockerfile_text: str,
    build_error: str,
    attempt: int,
    max_retries: int,
) -> AIRecoveryResponse:
    """Single AI call for a Dockerfile fix. Raises on AI failure."""
    user_prompt = (
        f"This Docker build failed (attempt {attempt}/{max_retries}). "
        f"Return a patched Dockerfile.\n\n"
        f"=== Dockerfile ===\n{dockerfile_text}\n\n"
        f"=== Build error ===\n{build_error[-4000:]}"  # cap stderr to last 4k
    )

    text, _job_id, _msgs = client.get_text(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format=ResponseFormat.JSON_SCHEMA,
        schema=AIRecoverySchema,
        use_message_history=False,
    )
    if not text or not text.strip():
        raise ValueError("AI recovery returned empty response")
    data = json.loads(text)
    return AIRecoveryResponse(**data)


def try_ai_recovery(
    client: BaseAIClient,
    dockerfile_path: Path,
    build_error: str,
    rebuild: Callable[[], None],
    max_retries: int = 2,
    on_status: Optional[Callable[[str], None]] = None,
) -> bool:
    """Attempt to fix a failing Docker build via AI, rebuilding each time.

    ``rebuild`` is a zero-arg callable that re-runs ``build_image`` for
    the current service. It should raise on failure and return cleanly
    on success.

    Returns True on success, False if all retries exhausted.
    """
    log = on_status or (lambda _msg: None)
    last_error = build_error

    for attempt in range(1, max_retries + 1):
        try:
            current_dockerfile = dockerfile_path.read_text()
        except FileNotFoundError:
            log(f"ai_recovery: Dockerfile not found at {dockerfile_path}, abort")
            return False

        log(f"ai_recovery: requesting AI fix (attempt {attempt}/{max_retries})")
        try:
            response = call_ai_recovery(
                client=client,
                dockerfile_text=current_dockerfile,
                build_error=last_error,
                attempt=attempt,
                max_retries=max_retries,
            )
        except Exception as e:
            log(f"ai_recovery: AI call failed ({e}); abort")
            return False

        if not response.dockerfile.strip():
            log("ai_recovery: AI returned empty Dockerfile; abort")
            return False

        # Backup the prior Dockerfile so the user can inspect what changed.
        backup = dockerfile_path.with_suffix(
            dockerfile_path.suffix + f".pre-ai-recovery-{attempt}"
        )
        backup.write_text(current_dockerfile)
        dockerfile_path.write_text(response.dockerfile)
        log(
            f"ai_recovery: wrote patched Dockerfile (prior backed up at {backup.name}). "
            f"AI explanation: {response.explanation[:200]}"
        )

        try:
            rebuild()
            log(f"ai_recovery: rebuild succeeded on attempt {attempt}")
            return True
        except Exception as e:
            last_error = str(e)
            log(f"ai_recovery: rebuild failed on attempt {attempt}: {e}")

    log(f"ai_recovery: exhausted {max_retries} retries")
    return False

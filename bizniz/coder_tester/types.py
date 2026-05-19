"""``coder_tester`` data types.

One agent call → one ``CoderTesterResult``. Same envelope shape as
``CoderAgentV3Result`` so downstream file-writing logic can stay
generic, but scoped to a single issue's target_files + test_files.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class FilledFile(BaseModel):
    """One file the agent wrote — either code or test, doesn't matter
    to the writer. The agent produces both in the same envelope."""
    path: str = Field(..., description="Workspace-relative path.")
    content: str = Field(..., description="Complete file content.")
    role: str = Field(
        default="code",
        description="Hint: 'code' or 'test'. Informational only.",
    )


class CoderTesterResult(BaseModel):
    """Output envelope from one CoderTesterAgent dispatch (one issue).

    The list MUST include every path in the issue's ``target_files``
    + ``test_files``. Agent is forbidden from inventing paths outside
    that set (enforced post-call).
    """
    issue_id: str = Field(..., description="Echoes the issue id for traceability.")
    filled_files: List[FilledFile] = Field(default_factory=list)
    notes: str = Field(
        default="",
        description=(
            "Optional one-line free-text the agent emits about "
            "deferred work, assumptions, or open questions. NOT used "
            "as a gate; just surfaced in logs."
        ),
    )

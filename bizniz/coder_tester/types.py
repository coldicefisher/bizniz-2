"""``coder_tester`` data types.

Two output modes:

  - **whole_file**: ``filled_files: [FilledFile(path, content)]``.
    Agent emits the COMPLETE new file content; bizniz overwrites.
    Right for IMPLEMENT (greenfield, no existing code to preserve).

  - **edit**: ``edits: [FileEdit(path, old_text, new_text)]``.
    Agent emits surgical patches; bizniz finds old_text and
    replaces with new_text in the existing file. Right for REPAIR
    (preserve unchanged code; eliminate the "fix-breaks-unrelated"
    regression class).

The result envelope can carry either or both — caller's responsibility
to pick the mode that matches its prompt.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class FilledFile(BaseModel):
    """One file the agent wrote (whole-file mode)."""
    path: str = Field(..., description="Workspace-relative path.")
    content: str = Field(..., description="Complete file content.")
    role: str = Field(
        default="code",
        description="Hint: 'code' or 'test'. Informational only.",
    )


class FileEdit(BaseModel):
    """One surgical edit (edit mode) — v4 fix B (2026-05-20).

    Bizniz finds ``old_text`` in the file and replaces it with
    ``new_text``. ``old_text`` MUST appear exactly ONCE in the file
    — the agent has to give enough surrounding context to locate
    the change uniquely. Multiple matches → ambiguity → error
    (we'd rather surface than guess wrong).
    """
    path: str = Field(..., description="Workspace-relative path.")
    old_text: str = Field(
        ...,
        description=(
            "Existing text to replace. Must be a unique substring of "
            "the file's current content — pad with surrounding lines "
            "if a short edit might match multiple places."
        ),
    )
    new_text: str = Field(
        ...,
        description="Replacement text.",
    )
    role: str = Field(
        default="code",
        description="Hint: 'code' or 'test'. Informational only.",
    )


class RequestedDep(BaseModel):
    """One dependency the agent wants added to the project.

    CTX-4 (2026-05-20): structured alternative to "agent edits
    requirements.txt mid-call". Both paths work; this one is
    explicit so the orchestrator can validate package names + log
    what was added.
    """
    name: str = Field(
        ...,
        description="Distribution name (pyjwt, python-dateutil, react, ...).",
    )
    version: str = Field(
        default="",
        description=(
            "Version specifier. Empty for latest stable. Examples: "
            "'^2.10' (npm), '==2.10.0' (python), '>=3.0' (python)."
        ),
    )
    purpose: str = Field(
        default="",
        description="One-line why (logged for operator visibility).",
    )
    language: str = Field(
        default="python",
        description="'python' or 'typescript'.",
    )


class CoderTesterResult(BaseModel):
    """Output envelope from one CoderTesterAgent dispatch (one issue).

    Whole-file mode: ``filled_files`` is populated, ``edits`` empty.
    Edit mode: ``edits`` is populated, ``filled_files`` empty.
    Either mode: ``requested_deps`` may carry structured dep adds.
    """
    issue_id: str = Field(..., description="Echoes the issue id for traceability.")
    filled_files: List[FilledFile] = Field(default_factory=list)
    edits: List[FileEdit] = Field(default_factory=list)
    requested_deps: List[RequestedDep] = Field(
        default_factory=list,
        description=(
            "CTX-4 (2026-05-20): structured dep additions. Orchestrator "
            "appends to requirements.txt / package.json, runs install + "
            "restart, then re-validates."
        ),
    )
    notes: str = Field(
        default="",
        description=(
            "Optional one-line free-text the agent emits about "
            "deferred work, assumptions, or open questions. NOT used "
            "as a gate; just surfaced in logs."
        ),
    )

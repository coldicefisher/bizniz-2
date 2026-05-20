"""Apply ``FileEdit`` patches to a workspace.

Constraints:
- ``old_text`` MUST appear in the file exactly once (uniqueness).
  Zero matches → file changed since the agent read it, OR the agent
  hallucinated. Multiple matches → ambiguous; the agent didn't pad
  with enough context. Both are surfaced loudly.
- Edits within a single ``apply_edits`` call are applied in order
  AFTER each prior edit has landed — so an edit can refer to text
  added by the previous edit. (Order is the agent's responsibility.)

Returns ``EditApplyReport`` with per-edit outcome so the caller can
decide what to do with partial failures.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from pydantic import BaseModel, Field

from bizniz.coder_tester.types import FileEdit
from bizniz.workspace.base_workspace import BaseWorkspace


class EditFailure(BaseModel):
    """One edit that couldn't be applied."""
    path: str
    reason: str
    old_text_preview: str = ""


class EditApplyReport(BaseModel):
    """Outcome of ``apply_edits()``."""
    paths_written: List[str] = Field(default_factory=list)
    failures: List[EditFailure] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def apply_edits(
    workspace: BaseWorkspace,
    edits: Iterable[FileEdit],
) -> EditApplyReport:
    """Apply each FileEdit to the workspace in order.

    Multi-edit semantics:
    - Edits to the same path apply sequentially against the current
      on-disk content (each edit sees the prior edit's result).
    - Edits to different paths apply independently.

    Failure semantics — recorded in ``report.failures`` but DON'T
    raise:
    - ``no_match``: old_text not in file
    - ``ambiguous``: old_text appears more than once
    - ``file_missing``: file doesn't exist
    - ``read_error``: file unreadable
    """
    report = EditApplyReport()
    # Track which paths we've touched so the in-memory buffer reflects
    # multi-edit ordering.
    buffers: dict = {}

    for edit in edits:
        path = edit.path
        # Get the current content — from in-memory buffer if we've
        # already touched it this call, else from disk.
        if path in buffers:
            content = buffers[path]
        else:
            try:
                p = workspace.path(path)
                if not p.exists() or not p.is_file():
                    report.failures.append(EditFailure(
                        path=path, reason="file_missing",
                        old_text_preview=edit.old_text[:80],
                    ))
                    continue
                content = p.read_text(encoding="utf-8")
            except Exception as e:
                report.failures.append(EditFailure(
                    path=path, reason=f"read_error: {type(e).__name__}: {e}",
                    old_text_preview=edit.old_text[:80],
                ))
                continue

        # Locate old_text. Must be unique.
        occurrences = content.count(edit.old_text)
        if occurrences == 0:
            report.failures.append(EditFailure(
                path=path, reason="no_match",
                old_text_preview=edit.old_text[:80],
            ))
            continue
        if occurrences > 1:
            report.failures.append(EditFailure(
                path=path, reason=f"ambiguous ({occurrences} matches)",
                old_text_preview=edit.old_text[:80],
            ))
            continue

        # Apply: single replacement.
        new_content = content.replace(edit.old_text, edit.new_text, 1)
        buffers[path] = new_content

    # Flush buffers to disk.
    for path, content in buffers.items():
        try:
            workspace.write_file(path, content)
            if path not in report.paths_written:
                report.paths_written.append(path)
        except Exception as e:
            report.failures.append(EditFailure(
                path=path, reason=f"write_error: {type(e).__name__}: {e}",
            ))

    return report

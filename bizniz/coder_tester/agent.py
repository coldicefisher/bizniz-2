"""``CoderTesterAgent`` — v4 single-call agent that writes code + tests
for ONE issue.

The unification of v2 Coder + v2 Tester into a single agent. Same
context produces both, no Coder/Tester drift. Per-issue scope makes
it parallelizable (see ``parallel_issue_runner.py``).

Structured output (no tool loop). Output paths are constrained to
exactly the issue's target_files + test_files.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from bizniz.architect.types import ServiceDefinition
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.coder.types import Issue
from bizniz.coder_tester.prompts import (
    CODER_TESTER_EDIT_SYSTEM_PROMPT,
    CODER_TESTER_SYSTEM_PROMPT,
    build_user_prompt,
)
from bizniz.coder_tester.types import (
    CoderTesterResult, FileEdit, FilledFile,
)
from bizniz.lib.llm_utils import call_with_retry
from bizniz.quality_engineer.types import CapabilitySpec


CODER_TESTER_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "coder_tester_output",
        "schema": {
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Echo back the issue id from the prompt.",
                },
                "filled_files": {
                    "type": "array",
                    "description": (
                        "One entry per path in the issue's target_files + "
                        "test_files. Code AND tests in one envelope."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Workspace-relative path.",
                            },
                            "content": {
                                "type": "string",
                                "description": (
                                    "Complete filled file. Imports + "
                                    "signatures preserved from the seed; "
                                    "bodies real. No NotImplementedError."
                                ),
                            },
                            "role": {
                                "type": "string",
                                "enum": ["code", "test"],
                                "description": "'code' or 'test'. Informational.",
                            },
                        },
                        "required": ["path", "content", "role"],
                        "additionalProperties": False,
                    },
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "Optional one-line free-text about deferred work, "
                        "assumptions, open questions. NOT used as a gate."
                    ),
                },
            },
            "required": ["issue_id", "filled_files", "notes"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


# v4 fix B (2026-05-20): edit-mode schema. The agent emits surgical
# patches instead of whole-file content. Bizniz applies each patch
# via ``apply_edits`` — unchanged code in the existing file stays
# verbatim. Right for REPAIR; whole-file stays right for IMPLEMENT
# (greenfield).
CODER_TESTER_EDIT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "coder_tester_edit_output",
        "schema": {
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Echo back the issue id from the prompt.",
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "Surgical patches. Each is a find/replace within "
                        "the current on-disk content. old_text MUST be "
                        "unique in the file — pad with surrounding "
                        "context if a short edit could match multiple places."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Workspace-relative path.",
                            },
                            "old_text": {
                                "type": "string",
                                "description": (
                                    "Existing substring to replace. Must "
                                    "be unique in the current file."
                                ),
                            },
                            "new_text": {
                                "type": "string",
                                "description": "Replacement text.",
                            },
                            "role": {
                                "type": "string",
                                "enum": ["code", "test"],
                                "description": "'code' or 'test'. Informational.",
                            },
                        },
                        "required": ["path", "old_text", "new_text", "role"],
                        "additionalProperties": False,
                    },
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "Optional one-line free-text about deferred work, "
                        "assumptions, open questions. NOT used as a gate."
                    ),
                },
            },
            "required": ["issue_id", "edits", "notes"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


class CoderTesterError(Exception):
    """Agent output failed validation or violated the path contract."""


class CoderTesterAgent:
    """Single-call coder+tester for one issue. v4 building block."""

    def __init__(
        self,
        client: BaseAIClient,
        on_status: Optional[Callable[[str], None]] = None,
        max_retries: int = 3,
    ):
        self._client = client
        self._on_status = on_status
        self._max_retries = max_retries

    def code_issue(
        self,
        *,
        issue: Issue,
        service: ServiceDefinition,
        seeded_files: List[FilledFile],
        capabilities: List[CapabilitySpec],
        skeleton_md: Optional[str] = None,
        auth_contract: Optional[str] = None,
        sibling_issue_summaries: Optional[List[str]] = None,
        # v4 fix B (2026-05-20): when True, the agent emits surgical
        # edits (FileEdit) instead of whole-file content. Use for
        # REPAIR; leave False for IMPLEMENT.
        edit_mode: bool = False,
    ) -> CoderTesterResult:
        """Code + test ONE issue end-to-end in one LLM call.

        ``edit_mode=False`` (default): agent emits ``filled_files``
        (whole-file content). Right for IMPLEMENT.

        ``edit_mode=True``: agent emits ``edits`` (surgical patches
        against existing files). Right for REPAIR — preserves
        unchanged code, eliminates whole-file overwrite regressions.

        Validates output paths against the issue's declared
        ``target_files`` + ``test_files`` union. Out-of-scope paths
        fail loudly.
        """
        mode_label = "edit" if edit_mode else "whole"
        self._log(
            f"CoderTesterAgent[{issue.id}, {mode_label}]: {issue.title} "
            f"({len(issue.target_files)} code file(s), "
            f"{len(issue.test_files)} test file(s))"
        )

        user_prompt = build_user_prompt(
            issue=issue,
            service=service,
            seeded_files=seeded_files,
            capabilities=capabilities,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
            sibling_issue_summaries=sibling_issue_summaries,
            edit_mode=edit_mode,
        )

        system_prompt = (
            CODER_TESTER_EDIT_SYSTEM_PROMPT if edit_mode
            else CODER_TESTER_SYSTEM_PROMPT
        )
        schema = (
            CODER_TESTER_EDIT_SCHEMA if edit_mode
            else CODER_TESTER_SCHEMA
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=schema,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label=f"CoderTesterAgent[{issue.id},{mode_label}]",
        )

        if edit_mode:
            return self._parse_edit_result(issue, raw)

        items = raw.get("filled_files") or []
        if not items:
            raise CoderTesterError(
                f"CoderTesterAgent[{issue.id}]: empty filled_files. "
                f"Refusing to ship a no-op result."
            )

        filled: List[FilledFile] = []
        for it in items:
            # Salvage common Haiku schema misses BEFORE Pydantic
            # validates. The strict JSON-schema path occasionally
            # produces ``role=null`` or omits the field on bigger
            # backend issues (observed on recipe_v4_v6 backend run,
            # 8/9 issues affected). Infer role from path extension
            # when missing/null; default the path to a salvageable
            # value if absolutely necessary.
            it = dict(it) if isinstance(it, dict) else it
            if isinstance(it, dict):
                if it.get("role") in (None, ""):
                    path_str = it.get("path", "") or ""
                    looks_like_test = (
                        "/test_" in path_str
                        or path_str.startswith("test_")
                        or "/tests/" in path_str
                        or path_str.endswith("_test.py")
                        or path_str.endswith(".test.tsx")
                        or path_str.endswith(".test.ts")
                    )
                    it["role"] = "test" if looks_like_test else "code"
            try:
                filled.append(FilledFile(**it))
            except Exception as e:
                # Capture the rich Pydantic ValidationError details
                # (errors() lists each field problem). Without this
                # the message is just "1 validation error for FilledFile"
                # with no indication of which field failed.
                detail = ""
                errors = getattr(e, "errors", None)
                if callable(errors):
                    try:
                        detail = f" — fields: {errors()}"
                    except Exception:
                        pass
                keys = list(it.keys()) if isinstance(it, dict) else "(non-dict)"
                raise CoderTesterError(
                    f"CoderTesterAgent[{issue.id}]: filled_files entry "
                    f"failed validation: {type(e).__name__}: {e}{detail}; "
                    f"item keys: {keys}"
                )

        # Path-contract gate: every produced path must be in the
        # issue's declared file set. Extras = scope violation;
        # missing = incomplete (warn, but don't fail — the per-issue
        # validator catches missing code/test files downstream).
        allowed = set(issue.target_files) | set(issue.test_files)
        produced = {f.path for f in filled}
        extras = produced - allowed
        if extras:
            raise CoderTesterError(
                f"CoderTesterAgent[{issue.id}]: agent wrote files outside "
                f"the issue's declared scope: {sorted(extras)}. "
                f"Allowed: {sorted(allowed)}."
            )
        missing = allowed - produced
        if missing:
            self._log(
                f"CoderTesterAgent[{issue.id}]: WARNING — declared paths "
                f"not produced: {sorted(missing)} (per-issue validator "
                f"will surface this)"
            )

        echoed_id = raw.get("issue_id") or ""
        if echoed_id and echoed_id != issue.id:
            self._log(
                f"CoderTesterAgent[{issue.id}]: WARNING — agent echoed "
                f"issue_id={echoed_id!r}, expected {issue.id!r}"
            )

        notes = raw.get("notes") or ""
        if notes:
            self._log(f"CoderTesterAgent[{issue.id}]: notes — {notes}")

        self._log(
            f"CoderTesterAgent[{issue.id}]: → {len(filled)} file(s) filled "
            f"({sum(1 for f in filled if f.role == 'code')} code, "
            f"{sum(1 for f in filled if f.role == 'test')} test)"
        )
        return CoderTesterResult(
            issue_id=issue.id, filled_files=filled, notes=notes,
        )

    def _parse_edit_result(
        self, issue: Issue, raw: dict,
    ) -> CoderTesterResult:
        """Parse the edit-mode envelope. Validates output paths
        against the issue's declared file set."""
        items = raw.get("edits") or []
        if not items:
            raise CoderTesterError(
                f"CoderTesterAgent[{issue.id}, edit]: empty edits. "
                f"Refusing to ship a no-op result."
            )

        edits: List[FileEdit] = []
        for it in items:
            it = dict(it) if isinstance(it, dict) else it
            if isinstance(it, dict) and it.get("role") in (None, ""):
                path_str = it.get("path", "") or ""
                looks_like_test = (
                    "/test_" in path_str
                    or path_str.startswith("test_")
                    or "/tests/" in path_str
                    or path_str.endswith("_test.py")
                    or path_str.endswith(".test.tsx")
                    or path_str.endswith(".test.ts")
                )
                it["role"] = "test" if looks_like_test else "code"
            try:
                edits.append(FileEdit(**it))
            except Exception as e:
                detail = ""
                errors = getattr(e, "errors", None)
                if callable(errors):
                    try:
                        detail = f" — fields: {errors()}"
                    except Exception:
                        pass
                keys = list(it.keys()) if isinstance(it, dict) else "(non-dict)"
                raise CoderTesterError(
                    f"CoderTesterAgent[{issue.id}, edit]: edits entry "
                    f"failed validation: {type(e).__name__}: {e}{detail}; "
                    f"item keys: {keys}"
                )

        # Path-contract gate (same as whole-file mode).
        allowed = set(issue.target_files) | set(issue.test_files)
        produced = {e.path for e in edits}
        extras = produced - allowed
        if extras:
            raise CoderTesterError(
                f"CoderTesterAgent[{issue.id}, edit]: agent wrote edits "
                f"outside the issue's declared scope: {sorted(extras)}. "
                f"Allowed: {sorted(allowed)}."
            )

        echoed_id = raw.get("issue_id") or ""
        if echoed_id and echoed_id != issue.id:
            self._log(
                f"CoderTesterAgent[{issue.id}, edit]: WARNING — agent "
                f"echoed issue_id={echoed_id!r}"
            )

        notes = raw.get("notes") or ""
        if notes:
            self._log(f"CoderTesterAgent[{issue.id}, edit]: notes — {notes}")

        self._log(
            f"CoderTesterAgent[{issue.id}, edit]: → {len(edits)} edit(s) "
            f"across {len(produced)} file(s)"
        )
        return CoderTesterResult(
            issue_id=issue.id, edits=edits, notes=notes,
        )

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

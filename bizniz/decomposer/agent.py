"""Decomposer agent — breaks one issue into ordered units of work."""
from __future__ import annotations

from typing import Callable, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.decomposer.prompts.decompose_prompt import (
    DECOMPOSE_SCHEMA,
    DECOMPOSE_SYSTEM_PROMPT,
    build_decompose_prompt,
)
from bizniz.decomposer.types import DecompositionResult, UnitOfWork
from bizniz.engineer.types import Issue
from bizniz.lib.llm_utils import call_with_retry


class DecomposerError(Exception):
    """Decomposer-specific failure (LLM output failed schema, empty
    decomposition, missing required field). Caller can fall back to
    treating the issue as a single unit (no decomposition)."""


class Decomposer:
    """Single-call agent that breaks an issue into ordered units.

    Same lifecycle shape as our other single-call agents (QualityEngineer,
    ServicePlanner, AuthPlanner): construct with a ``BaseAIClient``,
    call ``decompose(issue, service, architecture)`` per issue, get
    back a ``DecompositionResult``.
    """

    def __init__(
        self,
        client: BaseAIClient,
        on_status: Optional[Callable[[str], None]] = None,
        max_retries: int = 3,
    ):
        self._client = client
        self._on_status = on_status
        self._max_retries = max_retries

    # ── Public ───────────────────────────────────────────────────────

    def decompose(
        self,
        issue: Issue,
        service: ServiceDefinition,
        architecture: SystemArchitecture,
        existing_files_hint: Optional[str] = None,
    ) -> DecompositionResult:
        """Produce an ordered list of units for ``issue``.

        ``existing_files_hint`` is an optional workspace-state hint —
        callers that have a workspace summary on hand can pass it so
        the decomposer knows what's already there.

        Raises ``DecomposerError`` if the LLM returns a payload that
        fails schema validation, is empty, or has duplicate unit ids.
        """
        self._log(f"Decomposer: {issue.id} ({issue.title})")

        user_prompt = build_decompose_prompt(
            issue_id=issue.id,
            issue_title=issue.title,
            issue_description=issue.description,
            issue_target_files=issue.target_files,
            issue_success_criteria=issue.success_criteria,
            service_name=service.name,
            service_framework=service.framework or "unknown",
            architecture_summary=_summarize_architecture(architecture),
            existing_files_hint=existing_files_hint,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=DECOMPOSE_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=DECOMPOSE_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label=f"Decomposer.{issue.id}",
        )

        # Force the issue_id to match what we asked about. Models
        # occasionally restate it — pin to avoid downstream confusion.
        raw["issue_id"] = issue.id

        try:
            result = DecompositionResult.model_validate(raw)
        except Exception as e:
            raise DecomposerError(
                f"decompose({issue.id}): LLM output failed schema "
                f"validation: {e}"
            ) from e

        if not result.ordered_units:
            raise DecomposerError(
                f"decompose({issue.id}): returned zero units. Refusing "
                f"to ship an empty decomposition (caller should fall "
                f"back to single-unit dispatch)."
            )

        # Guard against duplicate unit ids — would silently break
        # downstream resume + dependency resolution.
        seen: set = set()
        dups: List[str] = []
        for u in result.ordered_units:
            if u.id in seen:
                dups.append(u.id)
            seen.add(u.id)
        if dups:
            raise DecomposerError(
                f"decompose({issue.id}): duplicate unit ids: {dups}"
            )

        self._log(
            f"Decomposer: {issue.id} → {len(result.ordered_units)} "
            f"unit(s), confidence={result.confidence:.2f}"
        )
        return result

    # ── Internals ────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass


def _summarize_architecture(arch: SystemArchitecture) -> str:
    """Compact text summary of a SystemArchitecture for the prompt.

    Same shape as the QE helper — service names, frameworks,
    languages, dependencies. Decomposer doesn't need full code.
    """
    lines = [f"Project: {arch.project_name} ({arch.project_slug})"]
    if arch.description:
        lines.append(f"Description: {arch.description}")
    lines.append("\nServices:")
    for s in arch.services:
        deps = ", ".join(s.depends_on) if s.depends_on else "—"
        lines.append(
            f"  - {s.name} ({s.service_type}/{s.framework}, "
            f"{s.language}, port {s.port}, depends_on: {deps}): "
            f"{s.description}"
        )
    return "\n".join(lines)

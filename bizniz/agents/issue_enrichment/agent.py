"""Issue Enrichment Agent — turn an Engineer-emitted issue into a
production-grade specification the Coder can implement without
ambiguity.

ONE generic agent — no per-stack specialization. The agent figures
out from the target file paths + problem statement + workspace
context what concerns matter for this specific ticket.

Architecturally sits between ``engineer.analyze`` and ``coder``:

    Engineer.analyze    → list[Issue]
    IssueEnrichmentAgent.enrich(issue, ctx)  → EnrichedIssue   ← us
    Coder                → reads enriched + writes code
"""
from __future__ import annotations

import json
from typing import Callable, List, Optional

from bizniz.agents.issue_enrichment.prompt import (
    ISSUE_ENRICHMENT_SYSTEM_PROMPT,
    ISSUE_ENRICHMENT_USER_PROMPT_TEMPLATE,
)
from bizniz.agents.issue_enrichment.types import EnrichedIssue
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message


def _format_files_block(files: List[dict], label: str) -> str:
    """``[{filepath, action}, ...]`` → readable block."""
    if not files:
        return f"{label}: (none)"
    lines = [f"{label}:"]
    for f in files:
        path = f.get("filepath", "?")
        action = f.get("action", "")
        suffix = f" ({action})" if action else ""
        lines.append(f"  - {path}{suffix}")
    return "\n".join(lines)


def _format_simple_list(items: List[str], label: str) -> str:
    if not items:
        return f"{label}: (none)"
    return f"{label}:\n" + "\n".join(f"  - {x}" for x in items)


def _format_workspace_context(workspace_files: Optional[List[str]]) -> str:
    """Compact summary of what's already in the workspace."""
    if not workspace_files:
        return ""
    lines = ["EXISTING FILES IN WORKSPACE (informational only):"]
    for p in workspace_files[:60]:
        lines.append(f"  - {p}")
    if len(workspace_files) > 60:
        lines.append(f"  ... ({len(workspace_files) - 60} more)")
    return "\n".join(lines)


def _format_auth_context(auth_context: Optional[str]) -> str:
    if not auth_context:
        return ""
    if len(auth_context) > 6000:
        auth_context = auth_context[:6000] + "\n[... truncated ...]"
    return f"AUTH CONTEXT (FusionAuth contract + spec):\n{auth_context}"


def _format_backend_contract(contract: Optional[dict]) -> str:
    if not contract:
        return ""
    # Pull the schemas section — the most useful slice for an
    # enrichment agent reasoning about field shapes.
    schemas = (
        (contract.get("components") or {}).get("schemas") or {}
    )
    if not schemas:
        return ""
    lines = ["BACKEND OPENAPI SCHEMAS (for cross-checking field names):"]
    for name in sorted(schemas):
        s = schemas[name] or {}
        props = list((s.get("properties") or {}).keys())
        required = s.get("required") or []
        marked = [
            f"{p}*" if p in required else p
            for p in props
        ]
        lines.append(f"  - {name}: {marked}  (* = required)")
    return "\n".join(lines)


class IssueEnrichmentAgent:
    """Single-call enrichment agent.

    Caller holds onto an instance per project / engineering pass and
    calls ``enrich()`` once per issue. The agent is stateless across
    calls — each enrichment is independent.
    """

    def __init__(
        self,
        client: BaseAIClient,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._client = client
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    def enrich(
        self,
        *,
        issue: dict,
        problem_statement: str,
        workspace_files: Optional[List[str]] = None,
        auth_context: Optional[str] = None,
        backend_openapi: Optional[dict] = None,
    ) -> EnrichedIssue:
        """Produce an EnrichedIssue for a single Engineer-emitted issue.

        ``issue`` is the raw dict the Engineer's analyze step produced
        (title, description, target_files, test_files, depends_on, ...).
        Soft-fails on AI errors: returns a minimal EnrichedIssue with
        confidence=low. The Coder still gets the original issue verbatim
        so engineering doesn't break on reviewer flake.
        """
        title = issue.get("title", "(untitled)")
        description = issue.get("description", "") or ""
        target_files = issue.get("target_files") or []
        test_files = issue.get("test_files") or []
        depends_on = issue.get("depends_on") or []

        user_prompt = ISSUE_ENRICHMENT_USER_PROMPT_TEMPLATE.format(
            issue_title=title,
            issue_description=description,
            target_files_block=_format_files_block(target_files, "TARGET FILES"),
            test_files_block=_format_simple_list(
                [t if isinstance(t, str) else (t.get("filepath", "?")) for t in test_files],
                "TEST FILES",
            ),
            depends_on_block=_format_simple_list(depends_on, "DEPENDS ON"),
            problem_statement=problem_statement.strip() or "(none)",
            auth_context_block=_format_auth_context(auth_context),
            workspace_context_block=_format_workspace_context(workspace_files),
            backend_contract_block=_format_backend_contract(backend_openapi),
        )

        self._log(f"Issue Enrichment: enriching '{title}'...")

        try:
            text, _job_id, _msgs = self._client.get_text(
                messages=[
                    Message(role="system", content=ISSUE_ENRICHMENT_SYSTEM_PROMPT),
                    Message(role="user", content=user_prompt),
                ],
                response_format=None,
            )
        except Exception as e:
            self._log(
                f"Issue Enrichment: AI call failed for '{title}' "
                f"({type(e).__name__}: {e}) — soft-failing"
            )
            return EnrichedIssue(
                original_issue_title=title,
                original_issue_description=description,
                confidence="low",
                notes=[f"Enrichment unavailable: {type(e).__name__}"],
            )

        return _parse_enrichment_response(
            text,
            issue_title=title,
            issue_description=description,
            on_status=self._on_status,
        )


_LIST_OF_STRING_FIELDS = (
    "validation_rules",
    "auth_requirements",
    "edge_cases",
    "test_scenarios",
    "dependencies_on_other_issues",
    "notes",
)

_ERROR_CASE_WHEN_ALIASES = ("condition", "trigger", "cause")
_ERROR_CASE_STATUS_ALIASES = ("status", "code")


def _coerce_enrichment_shape(data: dict) -> None:
    """In-place tolerant coercion for common AI output drift.

    Prompts can describe the schema, but small models still flake on
    list-vs-string and field-name aliases. We coerce here rather than
    soft-fail on validation, since the corrections are unambiguous.
    """
    # Common alias for the dependencies list field.
    if "dependencies" in data and "dependencies_on_other_issues" not in data:
        data["dependencies_on_other_issues"] = data.pop("dependencies")

    # Wrap stray strings in lists for fields that must be List[str].
    for field in _LIST_OF_STRING_FIELDS:
        v = data.get(field)
        if isinstance(v, str):
            data[field] = [v] if v.strip() else []
        elif v is None:
            data[field] = []

    # error_cases: rename `condition`/`trigger`/`cause` → `when`,
    # `status`/`code` → `status_code`.
    cases = data.get("error_cases")
    if isinstance(cases, list):
        for c in cases:
            if not isinstance(c, dict):
                continue
            if "when" not in c:
                for alias in _ERROR_CASE_WHEN_ALIASES:
                    if alias in c:
                        c["when"] = c.pop(alias)
                        break
            if "status_code" not in c:
                for alias in _ERROR_CASE_STATUS_ALIASES:
                    if alias in c:
                        try:
                            c["status_code"] = int(c.pop(alias))
                        except (ValueError, TypeError):
                            pass
                        break


def _parse_enrichment_response(
    text: str,
    *,
    issue_title: str,
    issue_description: str,
    on_status: Optional[Callable[[str], None]] = None,
) -> EnrichedIssue:
    """Tolerant JSON extraction. The model may wrap in fences or
    add prose — strip and find the first {...} block.
    """
    raw = text.strip()
    if raw.startswith("```"):
        first_nl = raw.find("\n")
        if first_nl != -1:
            raw = raw[first_nl + 1 :]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        if on_status:
            on_status(
                f"Issue Enrichment: response had no JSON object for "
                f"'{issue_title}' — soft-failing"
            )
        return EnrichedIssue(
            original_issue_title=issue_title,
            original_issue_description=issue_description,
            confidence="low",
            notes=["Enrichment response was unparseable"],
        )

    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as e:
        if on_status:
            on_status(
                f"Issue Enrichment: JSON parse failed for '{issue_title}' "
                f"({e}) — soft-failing"
            )
        return EnrichedIssue(
            original_issue_title=issue_title,
            original_issue_description=issue_description,
            confidence="low",
            notes=[f"JSON parse failed: {e}"],
        )

    # Defensive: ensure original fields are populated even if the
    # AI dropped them. They're load-bearing for traceability.
    data.setdefault("original_issue_title", issue_title)
    data.setdefault("original_issue_description", issue_description)

    _coerce_enrichment_shape(data)

    try:
        return EnrichedIssue.model_validate(data)
    except Exception as e:
        if on_status:
            on_status(
                f"Issue Enrichment: schema validation failed for "
                f"'{issue_title}' ({e}) — soft-failing"
            )
        return EnrichedIssue(
            original_issue_title=issue_title,
            original_issue_description=issue_description,
            confidence="low",
            notes=[f"Schema validation failed: {type(e).__name__}: {e}"],
        )

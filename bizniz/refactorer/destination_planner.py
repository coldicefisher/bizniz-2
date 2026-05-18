"""Agent-driven destination-path inference (D19 / v3 refactorer Step 2).

After a candidate passes the decision gate, ``DestinationPlanner``
runs ONE agent call (with file-discovery tools) to decide:

  - WHERE in ``core/python/`` the extracted code should land
  - Whether the destination is an EXISTING module (preferred —
    consolidates with related code) or a NEW module
  - What signature changes are needed (e.g. dependency injection
    for things the route had via FastAPI ``Depends``)
  - What import string the consumer needs after extraction

This replaces v2's heuristic ``_suggested_core_path``. The agent
reads the actual core/python/ layout, then picks a destination
that fits the existing structure — that means related extractions
cluster naturally instead of each getting its own snowflake
module.

Output (``DestinationPlan``) feeds into the executor's prompt:
"move this code to <destination_path>, rewrite consumers to
import via <consumer_import>, applying <signature_changes>."
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, List, Literal, Optional

from pydantic import BaseModel, Field


DestinationKind = Literal["existing", "new"]


class SignatureChange(BaseModel):
    """One change the executor must make to the extracted function's
    signature so it works as a domain function rather than a
    framework-bound handler."""
    parameter: str = Field(
        ...,
        description="Parameter name in the new signature.",
    )
    reason: str = Field(
        ...,
        description=(
            "Why this parameter is in the new signature — usually "
            "because it was a FastAPI Depends() in the original, "
            "or a request body field that needs to be a plain arg."
        ),
    )


class DestinationPlan(BaseModel):
    """The agent's decision for ONE candidate's extraction destination.

    The executor consumes this plus the candidate's source context
    to produce its prompt.
    """
    destination_path: str = Field(
        ...,
        description=(
            "Path RELATIVE to project root where the extracted code "
            "should land — e.g. 'core/python/recipes/pricing.py'."
        ),
    )
    destination_kind: DestinationKind = Field(
        ...,
        description=(
            "'existing' — destination_path already exists; APPEND to it. "
            "'new' — destination_path is a fresh module; CREATE it."
        ),
    )
    functions_to_move: List[str] = Field(
        default_factory=list,
        description=(
            "Names of functions/classes from the source that move "
            "to the destination. The executor uses these to scope "
            "its edits."
        ),
    )
    signature_changes: List[SignatureChange] = Field(
        default_factory=list,
        description=(
            "Adjustments needed because the extracted function moves "
            "from a framework-bound context (route handler / worker "
            "task) to a plain Python function in core."
        ),
    )
    consumer_import: str = Field(
        ...,
        description=(
            "Exact import line the consumer (the original route / "
            "worker) needs to add after extraction — e.g. "
            "'from python_core.recipes.pricing import compute_price'."
        ),
    )
    rationale: str = Field(
        ...,
        description=(
            "Why this destination + signature shape. Logged for audit."
        ),
    )


# ── Schema (for response_format-aware clients) ───────────────────


DESTINATION_PLAN_SCHEMA = {
    "type": "object",
    "required": [
        "destination_path", "destination_kind",
        "functions_to_move", "consumer_import", "rationale",
    ],
    "properties": {
        "destination_path": {"type": "string", "minLength": 1},
        "destination_kind": {"enum": ["existing", "new"]},
        "functions_to_move": {
            "type": "array",
            "items": {"type": "string"},
        },
        "signature_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["parameter", "reason"],
                "properties": {
                    "parameter": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "consumer_import": {"type": "string", "minLength": 1},
        "rationale": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}


_SYSTEM_PROMPT = """\
You decide where extracted code should land in the project's
``core/python/`` directory.

You have these tools: Read, Glob, Grep. Use them to discover the
existing core/python/ layout — DO NOT assume; check.

Your job, given:
  - The source file + lines being extracted
  - A summary of what the code does
  - The project root path

Decide:
  1. **destination_path** — relative-to-project path under
     ``core/python/``. Examples:
       - ``core/python/recipes/pricing.py``
       - ``core/python/auth/role_check.py``
       - ``core/python/billing/__init__.py`` (if the target is a
         package's __init__)
  2. **destination_kind** — ``existing`` if the file already exists
     (you read it to confirm); ``new`` otherwise. Prefer ``existing``
     when there's a thematically-related module already there —
     consolidation beats proliferation.
  3. **functions_to_move** — names of the functions/classes that
     migrate. Usually one or two; sometimes a small helper too.
  4. **signature_changes** — what changes when this code moves from
     its current home (route handler, worker task) to a plain
     Python function. Common cases:
       - FastAPI ``Depends(get_db)`` → ``db: AsyncSession`` plain arg
       - Pydantic request body → individual args or a typed dataclass
       - HTTP exceptions → domain exceptions raised back to the route
  5. **consumer_import** — exact import line the original file
     adds after extraction. Use the python_core mount path:
     ``from python_core.<package>.<module> import <symbol>``.
  6. **rationale** — one paragraph: why this destination + shape.

Constraints:

- ``destination_path`` MUST start with ``core/python/``.
- Module + filename + symbol naming follows Python conventions
  (snake_case files, PascalCase classes, snake_case functions).
- Don't propose paths that conflict with the framework's namespace
  (e.g. don't put domain code at ``core/python/fastapi.py``).
- If the candidate is a small helper that doesn't deserve its own
  module, propose appending to a thematically-related existing
  module rather than creating a new file.

Return VALID JSON matching the schema. Example output:

  {
    "destination_path": "core/python/recipes/pricing.py",
    "destination_kind": "existing",
    "functions_to_move": ["compute_total_with_tax"],
    "signature_changes": [
      {
        "parameter": "db",
        "reason": "was Depends(get_db) in route; now plain AsyncSession arg"
      }
    ],
    "consumer_import": "from python_core.recipes.pricing import compute_total_with_tax",
    "rationale": "pricing.py already has tax-rate helpers; this consolidates the tax math into one module rather than spawning a new pricing-secondary module."
  }
"""


_USER_TEMPLATE = """\
Extraction destination planning request.

**Project root:** {project_root}
**Candidate summary:** {summary}
**Candidate kind:** {kind}
**Source file:** {source_file}{line_block}
{suggested_block}

**Source snippet:**
```python
{snippet}
```

Use Read/Glob/Grep on ``{project_root}/core/python/`` to see the
existing layout, then decide the destination per your system
prompt. Return JSON.
"""


class DestinationPlanner:
    """Runs the agent-driven destination decision for one candidate.

    Like the decision gate, this is a single LLM call — but unlike
    the gate, it expects the LLM to have file-discovery tools so it
    can scan core/python/ before deciding. The ``llm_invoker``'s
    signature is the same as the gate's: ``(system, user) -> text``.
    Whether the underlying client has tool access is the caller's
    concern.

    Never raises — bad responses become a conservative ``new``-
    destination plan with the failure reason in rationale, so the
    pipeline can degrade rather than halt.
    """

    def __init__(
        self,
        project_root: Path,
        llm_invoker: Callable[[str, str], str],
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._invoke = llm_invoker
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    # ── Public ─────────────────────────────────────────────────────

    def plan_for(
        self,
        *,
        candidate_kind: str,
        summary: str,
        source_file: str,
        snippet: str,
        line_range: Optional[tuple] = None,
        suggested_path: Optional[str] = None,
    ) -> DestinationPlan:
        """Run the agent call and return the destination plan."""
        user_prompt = self._build_user_prompt(
            candidate_kind=candidate_kind,
            summary=summary,
            source_file=source_file,
            snippet=snippet,
            line_range=line_range,
            suggested_path=suggested_path,
        )
        try:
            raw = self._invoke(_SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            self._log(
                f"DestinationPlanner: llm invoke raised "
                f"{type(e).__name__}: {e}"
            )
            return self._fallback_plan(
                summary=summary,
                reason=f"planner LLM failed: {type(e).__name__}: {e}",
            )
        plan = _parse_plan(raw)
        if plan is None:
            self._log(
                f"DestinationPlanner: response parse failed; using "
                f"fallback for {summary!r}"
            )
            return self._fallback_plan(
                summary=summary,
                reason="planner response failed to parse",
            )
        # Hard sanity check: destination must be under core/python/.
        if not plan.destination_path.startswith("core/python/"):
            self._log(
                f"DestinationPlanner: rejecting out-of-scope "
                f"destination {plan.destination_path!r}; using fallback"
            )
            return self._fallback_plan(
                summary=summary,
                reason=(
                    f"agent proposed out-of-scope destination "
                    f"{plan.destination_path!r}; must be under "
                    f"core/python/"
                ),
            )
        return plan

    def _fallback_plan(
        self, *, summary: str, reason: str,
    ) -> DestinationPlan:
        """Conservative default when the agent fails. New module
        under ``core/python/uncategorized/`` so the executor still
        has somewhere to land the code; the rationale carries the
        failure reason."""
        # Derive a slug from the summary.
        slug = "".join(
            c if c.isalnum() else "_" for c in summary.lower()
        ).strip("_")[:40] or "extracted"
        return DestinationPlan(
            destination_path=f"core/python/uncategorized/{slug}.py",
            destination_kind="new",
            functions_to_move=[],
            signature_changes=[],
            consumer_import=(
                f"from python_core.uncategorized.{slug} import *"
            ),
            rationale=(
                f"fallback (planner failed: {reason}) — "
                f"executor should review before committing"
            ),
        )

    @staticmethod
    def _build_user_prompt(
        *,
        candidate_kind: str,
        summary: str,
        source_file: str,
        snippet: str,
        line_range: Optional[tuple] = None,
        suggested_path: Optional[str] = None,
    ) -> str:
        line_block = ""
        if line_range:
            line_block = f" (lines {line_range[0]}-{line_range[1]})"
        suggested_block = ""
        if suggested_path:
            suggested_block = (
                f"\n**Heuristic suggestion (deterministic planner):** "
                f"{suggested_path}\n"
            )
        return _USER_TEMPLATE.format(
            project_root="<project_root>",  # placeholder; tools resolve real path
            summary=summary,
            kind=candidate_kind,
            source_file=source_file,
            line_block=line_block,
            suggested_block=suggested_block,
            snippet=(snippet or "(no snippet)")[:3000],
        )


# ── Parsing ──────────────────────────────────────────────────────


def _parse_plan(raw_text: str) -> Optional[DestinationPlan]:
    """Pull a DestinationPlan from the LLM response. Returns None
    on shape failures — the caller falls back to a safe default."""
    text = (raw_text or "").strip()
    if not text:
        return None
    # Strip code fences.
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start: end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    try:
        return DestinationPlan.model_validate(data)
    except Exception:
        return None

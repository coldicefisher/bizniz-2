"""DocumentRecovery — single-session agent that writes missing
critical docs.

Sister to ``SmokeRecovery`` (D14). Shares the
``AgenticPhaseRecovery`` plumbing — this module owns the
docs-focused system prompt + user-message format.

When ``MilestoneLoop`` runs ``HumanDocsGenerator`` and one or more
critical docs end up missing (``architecture.md``,
``infrastructure.md``, ``auth.md``, or ``api/<svc>.md`` per
backend), the milestone loop dispatches this agent. It gets one
Claude CLI session with file + bash tools, the list of
missing paths, and the source data the generator would have used
(architecture JSON, compose YAML, openapi per service). It writes
the missing files.

On return, the milestone loop re-checks the critical docs list.
The iterative loop + ProgressTracker live in
``MilestoneLoop._maybe_recover_document``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.lib.agentic_phase_recovery import (
    AgenticPhaseRecovery,
    DEFAULT_TIMEOUT_S as _DEFAULT_TIMEOUT_S,
)


class DocumentRecovery(AgenticPhaseRecovery):
    """Single-shot agent that writes missing critical docs.

    Inherits CLI plumbing from ``AgenticPhaseRecovery``. The
    docs-focused system prompt is the class-level ``system_prompt``;
    this class overrides ``build_user_prompt`` to format the
    missing-doc paths + generator-input context the agent needs.
    """

    label = "DocumentRecovery"

    def __init__(
        self,
        project_root: Path,
        command: str = "claude",
        timeout_seconds: float = _DEFAULT_TIMEOUT_S,
        on_status: Optional[Callable[[str], None]] = None,
        fallback_model: Optional[str] = None,
    ) -> None:
        super().__init__(
            project_root=project_root,
            command=command,
            timeout_seconds=timeout_seconds,
            on_status=on_status,
            fallback_model=fallback_model,
        )

    system_prompt = """\
You are a documentation-recovery agent for the bizniz build pipeline.

The HumanDocsGenerator phase just finished. The harness now checks
that the **critical docs** exist under ``<project>/docs/``:

  - ``architecture.md`` — high-level service topology
  - ``infrastructure.md`` — compose stack + persistence + auth
  - ``auth.md`` — auth contract pointer
  - ``api/<service>.md`` — per-backend API reference

One or more of these is missing or empty. Your job: write the
missing docs from the source data the generator would have used.
You are running inside an iterative recovery loop — the harness
will keep dispatching you as long as the missing-count keeps
dropping, so bias toward writing solid files, not toward "this is
too complex." A genuinely-unfixable case stops itself when your
work stops landing.

You have these tools: Read, Write, Edit, Glob, Grep, Bash. Use the
discovery tools (Read/Glob/Grep) explicitly to locate the source
inputs — don't assume the workspace is already loaded:

  - The SystemArchitecture lives at
    ``.bizniz/runs/<job_id>/architecture.json`` (find the most
    recent run dir).
  - The compose file is at ``infra/development/docker-compose.yml``.
  - Captured OpenAPI per service is under
    ``.bizniz/runs/<job_id>/m<N>/contracts/<svc>.openapi.json``.
  - Existing docs (the ones that DID generate) are under ``docs/``;
    read them to match style/tone.

Constraints:

- Mirror the structure + heading shape of any docs that DID
  generate successfully (e.g. ``README.md``, ``services/<svc>.md``).
  Consistency matters more than cleverness.
- Stay deterministic where the data is structured — list services
  + ports + frameworks from architecture.json verbatim. Don't
  invent details.
- LLM-narrative is fine for the prose sections of architecture.md
  (rationale, trade-offs) — write a paragraph that fits the
  project. Don't pad.
- Each generated file must be a valid Markdown document with at
  least one H1 heading and a non-empty body.

End your final assistant message with ONE of:
  - ``ACTION: wrote docs/<path>`` (one per file)
  - ``RECOVERY SUCCESS: <one-paragraph summary>`` when every missing
    file is written
  - ``RECOVERY FAILED: <one-paragraph reason>`` if you couldn't
    produce one of them (e.g. the architecture.json itself is corrupt)

The harness will independently re-check the critical docs list
after your return — RECOVERY SUCCESS is your claim, but the
re-check is the truth.
"""

    def build_user_prompt(
        self,
        *,
        missing_critical_docs: List[str],
        services: List[Dict[str, str]],
        milestone_name: str,
        runs_root: Optional[str] = None,
    ) -> str:
        missing_block = "\n".join(f"  - docs/{p}" for p in missing_critical_docs)
        services_block = "\n".join(
            f"  - {s.get('name')} ({s.get('framework')}/{s.get('language')}, "
            f"port {s.get('port')})"
            for s in services
        )
        runs_hint = (
            f"  - structured artifacts (architecture.json, compose snapshot, "
            f"openapi per service) under: {runs_root}"
            if runs_root else ""
        )
        return _USER_TEMPLATE.format(
            milestone_name=milestone_name,
            missing_block=missing_block,
            services_block=services_block,
            project_root=str(self._project_root),
            runs_hint=runs_hint,
        )

    def recover(
        self,
        missing_critical_docs: List[str],
        services: List[Dict[str, str]],
        milestone_name: str,
        runs_root: Optional[str] = None,
    ):
        """Type-hinted entry point — base class handles dispatch."""
        return super().recover(
            missing_critical_docs=missing_critical_docs,
            services=services,
            milestone_name=milestone_name,
            runs_root=runs_root,
        )


_USER_TEMPLATE = """\
DOCS RECOVERY — milestone: {milestone_name}

HumanDocsGenerator finished but these CRITICAL docs are missing
or empty:

{missing_block}

Project services:

{services_block}

Stack context:
  - project root: {project_root}
{runs_hint}

Use the discovery tools to locate source inputs (architecture
JSON, compose YAML, openapi snapshots), then write each missing
doc. Follow the convention of any successfully-generated docs
under ``docs/`` for tone + structure.

Return ACTION lines for each file written, then either
RECOVERY SUCCESS or RECOVERY FAILED per your system prompt.
"""

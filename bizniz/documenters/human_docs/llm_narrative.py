"""LLM-driven narrative doc generation.

Where ``deterministic.py`` renders structured data (tables, graphs)
from architecture artifacts, this module asks an LLM to write
HUMAN narrative: README, quickstart, per-service descriptions,
milestone summaries.

The LLM invocation is injectable for tests. Production uses Claude
CLI subprocess. Failed LLM calls produce a fallback stub that the
operator can fill in manually — never crash the docs phase.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture


# ── Prompts ──────────────────────────────────────────────────────


_README_SYSTEM_PROMPT = """You are a senior technical writer producing a top-level README.md for a multi-service application.

You will be given the project's architecture (services, frameworks, dependencies) + the original problem statement. Write a README that:

1. Opens with a 1-2 sentence project description anchored in WHAT it does for users
2. Lists the services + what each is responsible for (one line each)
3. Has a "Getting started" section pointing readers to `docs/quickstart.md` (don't try to duplicate it here)
4. Has a "Documentation" section linking to other docs in `docs/` (architecture, infrastructure, auth, api/)
5. Has a "Tech stack" section listing frameworks/languages used

Output the Markdown content directly. No commentary. No JSON envelope. Just the README.
"""


_QUICKSTART_SYSTEM_PROMPT = """You are writing the quickstart guide for a multi-service application.

You will be given the architecture + the dev-mode docker-compose summary. Write a quickstart that:

1. Lists prerequisites (Docker, Docker Compose, Make, what versions if relevant)
2. Shows the single command to bring up the dev stack
3. Lists the URLs the user can hit (frontend, backend /docs, auth admin, db port) — based on the services + ports in the architecture
4. Shows how to seed a test user (point to AUTH_CONTRACT.md)
5. Shows how to run tests (pytest for Python services, npm test for TS services)
6. Has a "Troubleshooting" section with the 2-3 most common issues (compose conflicts, port collisions, db migrations)

Output the Markdown content directly. No commentary, no JSON.
"""


_SERVICE_SYSTEM_PROMPT = """You are documenting ONE service in a multi-service application.

You will be given:
- The service's metadata (name, framework, language, port, description, depends_on)
- The full project architecture (so you can describe how this service fits)

Write a per-service doc that:

1. Opens with the service's purpose in 1-2 sentences
2. Lists the responsibilities (what this service owns, what it does NOT own)
3. Describes the interfaces (HTTP routes for backend, UI surfaces for frontend, queues for worker)
4. Lists the upstream/downstream services it depends on or serves
5. Has a "How to extend" section pointing at the skeleton's extension points (e.g. add a new route by creating a file in `app/api/routes/`, add a frontend route in `src/routes/`, etc.)

Output the Markdown content directly. No commentary, no JSON.
"""


_MILESTONE_SYSTEM_PROMPT = """You are documenting ONE milestone of a phased build.

You will be given the milestone's name, problem slice, use cases, success criteria, and the enriched capability spec. Write a doc that:

1. Opens with what this milestone delivered for users
2. Lists the capabilities (CRUD entities, flows, dashboards) introduced in this milestone
3. Lists the success criteria + how they were verified
4. References the git tag (`m<N>-done`) and relevant test directories

Output the Markdown content directly. No commentary, no JSON.
"""


# ── Result types ─────────────────────────────────────────────────


class NarrativeResult:
    """Outcome of one LLM doc-generation call."""
    def __init__(self, content: str, succeeded: bool, error: Optional[str] = None):
        self.content = content
        self.succeeded = succeeded
        self.error = error


def _fallback_stub(label: str, reason: str) -> str:
    """Generate a stub doc the operator can fill in manually."""
    return (
        f"# {label}\n\n"
        f"_(Auto-generation didn't produce a narrative — "
        f"{reason}. Fill this in by hand or re-run the docs phase.)_\n"
    )


# ── Narrative writer ─────────────────────────────────────────────


class NarrativeWriter:
    """Wraps an LLM invoker for narrative doc generation.

    ``llm_invoker`` is injectable: takes (system_prompt, user_prompt)
    and returns the raw text response (or None on failure). Tests
    pass a fake; production uses Claude CLI subprocess.
    """

    def __init__(
        self,
        command: str = "claude",
        on_status: Optional[Callable[[str], None]] = None,
        llm_invoker: Optional[Callable[[str, str], Optional[str]]] = None,
        additional_args: Optional[List[str]] = None,
        timeout_s: float = 180.0,
    ) -> None:
        self._command = command
        self._on_status = on_status
        self._llm_invoker = llm_invoker or self._default_llm_invoker
        self._additional_args = list(additional_args or [])
        self._timeout_s = timeout_s

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def write_readme(
        self,
        arch: SystemArchitecture,
        problem_statement: str = "",
    ) -> NarrativeResult:
        user_prompt = (
            f"Project: {arch.project_name} ({arch.project_slug})\n\n"
            f"Description: {arch.description}\n\n"
            f"Original problem statement:\n{problem_statement}\n\n"
            f"Services:\n"
            + "\n".join(
                f"- {s.name} ({s.service_type}, {s.framework}, "
                f"{s.language}) — {s.description or '(no description)'}"
                for s in arch.services
            )
        )
        return self._invoke("README.md", _README_SYSTEM_PROMPT, user_prompt)

    def write_quickstart(
        self,
        arch: SystemArchitecture,
        compose_summary: str = "",
    ) -> NarrativeResult:
        user_prompt = (
            f"Project: {arch.project_name}\n\n"
            f"Services with ports:\n"
            + "\n".join(
                f"- {s.name}: port {s.port} ({s.framework})"
                for s in arch.services
                if s.port
            )
            + (f"\n\nCompose summary:\n{compose_summary}" if compose_summary else "")
        )
        return self._invoke("quickstart.md", _QUICKSTART_SYSTEM_PROMPT, user_prompt)

    def write_service(
        self,
        svc: ServiceDefinition,
        arch: SystemArchitecture,
    ) -> NarrativeResult:
        user_prompt = (
            f"Service to document: {svc.name}\n\n"
            f"Service metadata:\n"
            f"- type: {svc.service_type}\n"
            f"- framework: {svc.framework}\n"
            f"- language: {svc.language}\n"
            f"- port: {svc.port}\n"
            f"- depends_on: {', '.join(svc.depends_on or []) or '(none)'}\n"
            f"- description: {svc.description or '(none)'}\n\n"
            f"Full project ({arch.project_name}) has these services:\n"
            + "\n".join(
                f"- {s.name} ({s.service_type})"
                for s in arch.services
            )
        )
        return self._invoke(
            f"services/{svc.name}.md",
            _SERVICE_SYSTEM_PROMPT,
            user_prompt,
        )

    def write_milestone(
        self,
        milestone_index: int,
        milestone_name: str,
        milestone_problem_slice: str = "",
        capabilities_summary: str = "",
    ) -> NarrativeResult:
        user_prompt = (
            f"Milestone {milestone_index}: {milestone_name}\n\n"
            f"Problem slice:\n{milestone_problem_slice}\n\n"
            f"Capabilities delivered:\n{capabilities_summary}\n\n"
            f"Git tag: m{milestone_index}-done"
        )
        return self._invoke(
            f"milestones/m{milestone_index}.md",
            _MILESTONE_SYSTEM_PROMPT,
            user_prompt,
        )

    # ── Internals ────────────────────────────────────────────────

    def _invoke(
        self,
        label: str,
        system_prompt: str,
        user_prompt: str,
    ) -> NarrativeResult:
        self._log(f"NarrativeWriter: generating {label}...")
        try:
            content = self._llm_invoker(system_prompt, user_prompt)
        except Exception as e:
            self._log(
                f"NarrativeWriter: {label} llm_invoker raised "
                f"{type(e).__name__}: {e} — using fallback stub"
            )
            return NarrativeResult(
                content=_fallback_stub(label, f"LLM call raised {type(e).__name__}"),
                succeeded=False,
                error=str(e),
            )
        if not content or not content.strip():
            self._log(
                f"NarrativeWriter: {label} llm returned empty — using stub"
            )
            return NarrativeResult(
                content=_fallback_stub(label, "LLM returned empty"),
                succeeded=False,
                error="empty LLM response",
            )
        return NarrativeResult(content=content, succeeded=True)

    def _default_llm_invoker(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> Optional[str]:
        if shutil.which(self._command) is None:
            self._log(f"NarrativeWriter: {self._command!r} not on PATH")
            return None
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", system_prompt,
            "--permission-mode", "bypassPermissions",
        ] + self._additional_args
        try:
            proc = subprocess.run(
                cmd, input=user_prompt, capture_output=True,
                text=True, timeout=self._timeout_s,
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        try:
            envelope = json.loads(proc.stdout)
        except Exception:
            return None
        inner = envelope.get("result")
        return inner if isinstance(inner, str) else None

"""Architect — service-decomposition agent.

v2 single-call agent. Two methods:

  - ``decompose(problem_statement, project_name)`` — greenfield: take a
    problem statement and produce a ``SystemArchitecture``.
  - ``evolve(milestone, existing_architecture, ...)`` — milestone walk:
    take an existing architecture plus a milestone slice and return the
    architecture for after this milestone (existing services preserved).

The Architect does NOT walk milestones, dispatch implementers, run
integration tests, or coordinate the build. Those concerns live in the
pipeline driver (yet to be built) — the Architect itself is just the
LLM-call layer that turns problem statements into service decompositions.

Plain class, no inheritance, no tools. The shared retry+JSON-parse
helper from ``bizniz.lib.llm_utils`` handles the LLM round-trip.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.architect.prompts.decompose_prompt import DECOMPOSE_PROMPT_TEMPLATE
from bizniz.architect.prompts.evolve_prompt import build_evolve_prompt
from bizniz.architect.prompts.evolve_schema import EvolveArchitectSchema
from bizniz.architect.prompts.schema import ArchitectSchema
from bizniz.architect.prompts.system_prompt import ARCHITECT_SYSTEM_PROMPT
from bizniz.architect.types import (
    ArchitectBadAIResponseError,
    ServiceDefinition,
    SystemArchitecture,
)
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.lib.llm_utils import call_with_retry
from bizniz.workspace.naming import slugify


class Architect:
    """Decomposes a problem statement into a service-based architecture.

    Single-call agent. ``decompose`` and ``evolve`` are both one LLM
    round-trip each (modulo retries). No tools, no inheritance.
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

    # ── Public API ────────────────────────────────────────────────────────────

    def decompose(
        self, problem_statement: str, project_name: str,
    ) -> SystemArchitecture:
        """Greenfield decomposition: produce the initial architecture."""
        project_slug = slugify(project_name)
        self._log(f"Architect: decomposing '{project_name}' into services...")

        user_prompt = DECOMPOSE_PROMPT_TEMPLATE.format(
            problem_statement=problem_statement,
            project_name=project_name,
            project_slug=project_slug,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=ARCHITECT_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=ArchitectSchema,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="Architect.decompose",
        )

        # Defensive: the LLM occasionally returns a list at the top
        # level. Two real shapes seen in the wild:
        #   1. [{project_name, services: [...]}]  (response wrapped
        #      in a single-element list)
        #   2. [{name, service_type, framework, ...}, ...]  (just the
        #      services list, no wrapper dict)
        # Detect by peeking at the first element's shape.
        if isinstance(raw, list):
            self._log(
                f"Architect: LLM returned a list (len={len(raw)}); "
                f"attempting to normalize"
            )
            if (len(raw) == 1 and isinstance(raw[0], dict)
                    and "services" in raw[0]):
                raw = raw[0]  # unwrap shape 1
            else:
                raw = {"services": raw}  # shape 2: it's the services list
        services = [ServiceDefinition(**svc) for svc in raw.get("services") or []]
        if not services:
            raise ArchitectBadAIResponseError(
                "Architect returned no services — refusing to ship empty architecture."
            )

        # Greenfield: every service is "new"
        for s in services:
            s.evolve_state = "new"

        architecture = SystemArchitecture(
            project_name=raw.get("project_name") or project_name,
            project_slug=raw.get("project_slug") or project_slug,
            services=services,
            docker_compose=raw.get("docker_compose"),
            description=raw.get("description") or "",
        )

        self._log(
            f"Architect: architecture designed — {len(services)} services: "
            + ", ".join(s.name for s in services)
        )
        return architecture

    def evolve(
        self,
        milestone,
        existing_architecture: SystemArchitecture,
        problem_statement: str,
        project_name: str,
        project_root: Optional[Path] = None,
    ) -> SystemArchitecture:
        """Re-decompose for one milestone, preserving existing services.

        Every service in ``existing_architecture`` appears in the result
        tagged ``unchanged`` or ``extended``. New services are tagged
        ``new``. ``project_root`` is optional — when provided, the
        Architect can read concrete extension points (existing routes,
        schemas, store members) from the workspace docs so it doesn't
        have to imagine them.
        """
        project_slug = slugify(project_name)

        existing_services_block = self._format_existing_services(existing_architecture)
        use_cases_block = "\n".join(
            f"    - {uc}" for uc in (milestone.use_cases or [])
        ) or "    (none specified)"
        success_block = "\n".join(
            f"    - {sc}" for sc in (milestone.success_criteria or [])
        ) or "    (none specified)"

        workspace_state_block = self._read_workspace_state(project_root)

        self._log(
            f"Architect: evolving '{project_name}' for milestone "
            f"'{milestone.name}'..."
        )

        user_prompt = build_evolve_prompt(
            project_name=project_name,
            project_slug=project_slug,
            problem_statement=problem_statement,
            existing_services=existing_services_block,
            milestone_name=milestone.name,
            milestone_effort=milestone.estimated_effort or "",
            milestone_problem_slice=milestone.problem_slice,
            use_cases_block=use_cases_block,
            success_criteria_block=success_block,
            workspace_state_block=workspace_state_block,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=ARCHITECT_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=EvolveArchitectSchema,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label="Architect.evolve",
        )

        evolved = self._merge_evolve(
            raw, existing_architecture, project_name, project_slug,
        )

        new_count = sum(1 for s in evolved.services if s.evolve_state == "new")
        ext_count = sum(1 for s in evolved.services if s.evolve_state == "extended")
        self._log(
            f"Architect: milestone '{milestone.name}' → "
            f"{new_count} new + {ext_count} extended service(s) "
            f"(total {len(evolved.services)})"
        )
        return evolved

    # ── Internals ────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    @staticmethod
    def _format_existing_services(arch: SystemArchitecture) -> str:
        if not arch.services:
            return "  (none — fresh project)"
        lines = []
        for s in arch.services:
            depends = ", ".join(s.depends_on) if s.depends_on else "(none)"
            lines.append(
                f"  - {s.name}: type={s.service_type}, framework={s.framework}, "
                f"language={s.language}, port={s.port}, "
                f"skeleton={s.skeleton}, depends_on=[{depends}]"
            )
        return "\n".join(lines)

    def _read_workspace_state(self, project_root: Optional[Path]) -> str:
        """Read the project's docs to surface concrete extension points
        (existing routes, schemas, store members). Empty string when
        we're on M1 or no project_root is provided."""
        if project_root is None:
            return ""
        try:
            from bizniz.architect.workspace_reader import (
                format_existing_workspace_state,
            )
            return format_existing_workspace_state(project_root)
        except Exception as e:
            self._log(
                f"Architect: workspace state read failed "
                f"({type(e).__name__}: {e})"
            )
            return ""

    def _merge_evolve(
        self,
        raw: dict,
        existing_architecture: SystemArchitecture,
        project_name: str,
        project_slug: str,
    ) -> SystemArchitecture:
        """Merge the AI's evolve output with the existing architecture.

        Existing services keep their identity-bearing fields
        (workspace_name, port, image_name, skeleton). The AI may extend
        their depends_on / requirements / description. New services are
        accepted whole. Any existing service the AI dropped is restored
        as unchanged — defending against a class of regressions where a
        weak model "forgets" services on retry.
        """
        existing_by_name = {s.name: s for s in existing_architecture.services}
        services: List[ServiceDefinition] = []

        for svc_raw in raw.get("services") or []:
            state = svc_raw.get("evolve_state") or "unchanged"
            name = svc_raw["name"]
            if name in existing_by_name:
                prior = existing_by_name[name]
                merged = ServiceDefinition(
                    name=name,
                    service_type=prior.service_type,
                    framework=prior.framework,
                    language=prior.language,
                    description=svc_raw.get("description") or prior.description,
                    workspace_name=prior.workspace_name,
                    port=prior.port,
                    depends_on=list(svc_raw.get("depends_on") or prior.depends_on),
                    requirements=list(
                        dict.fromkeys(
                            list(prior.requirements or [])
                            + list(svc_raw.get("requirements") or [])
                        )
                    ),
                    skeleton=prior.skeleton,
                    image_name=prior.image_name,
                    evolve_state=state if state in ("extended", "unchanged") else "unchanged",
                )
                services.append(merged)
            else:
                fresh = ServiceDefinition(
                    name=name,
                    service_type=svc_raw["service_type"],
                    framework=svc_raw["framework"],
                    language=svc_raw["language"],
                    description=svc_raw["description"],
                    workspace_name=svc_raw["workspace_name"],
                    port=svc_raw.get("port"),
                    depends_on=list(svc_raw.get("depends_on") or []),
                    requirements=list(svc_raw.get("requirements") or []),
                    skeleton=svc_raw.get("skeleton") or "none",
                    evolve_state="new",
                )
                services.append(fresh)

        # Restore any service the AI dropped
        returned_names = {s.name for s in services}
        for prior in existing_architecture.services:
            if prior.name not in returned_names:
                clone = prior.model_copy(deep=True)
                clone.evolve_state = "unchanged"
                services.append(clone)
                self._log(
                    f"Architect: evolve dropped service '{prior.name}' — "
                    f"restoring as unchanged"
                )

        return SystemArchitecture(
            project_name=raw.get("project_name") or project_name,
            project_slug=raw.get("project_slug") or project_slug,
            services=services,
            docker_compose=None,
            description=raw.get("description") or existing_architecture.description,
        )

"""
Architect

Takes a problem statement and project name, decomposes the system into
containerized services, creates the project directory structure, generates
Dockerfiles and docker-compose.yml, builds Docker images, and dispatches
Engineer instances for application services.

Project structure:
    project_root/
    ├── .bizniz/project.db
    ├── backend/                  (service source code)
    │   ├── src/...
    │   └── tests/...
    ├── frontend/                 (service source code)
    │   ├── src/...
    │   └── tests/...
    └── infra/
        └── development/
            ├── docker-compose.yml
            ├── .env
            ├── backend/          (Dockerfile, requirements)
            └── frontend/         (Dockerfile)
"""

import concurrent.futures
import datetime
import json
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Callable, List, TYPE_CHECKING

from bizniz.base_ai_agent import BaseAIAgent
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.clients.errors import AIInsufficientFunds
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.workspace.naming import slugify

from bizniz.architect.types import (
    ServiceDefinition,
    SystemArchitecture,
    ServiceResult,
    ArchitectResult,
    ArchitectBadAIResponseError,
)
from bizniz.architect.prompts.system_prompt import ARCHITECT_SYSTEM_PROMPT
from bizniz.architect.prompts.decompose_prompt import DECOMPOSE_PROMPT_TEMPLATE
from bizniz.architect.prompts.schema import ArchitectSchema

if TYPE_CHECKING:
    from bizniz.provisioner import Provisioner


# Service types that are application code (need workspaces + engineers)
_APPLICATION_TYPES = {"backend", "frontend", "worker"}

# Service types that are infrastructure (use standard images, no workspace)
_INFRASTRUCTURE_TYPES = {"database", "cache", "proxy", "auth"}


class Architect(BaseAIAgent):
    """
    System architect agent.

    decompose(problem_statement, project_name) → SystemArchitecture
        AI decomposes the problem into services/containers.

    build(problem_statement, project_name) → ArchitectResult
        Full pipeline: decompose → create project structure → generate Docker
        configs → build images → dispatch engineers for each application service.

    Parameters
    ----------
    engineer_factory:
        Callable(workspace, on_status_message, image_name) → Engineer context manager.
    project_parent:
        Parent directory where the project root is created.
    """

    def __init__(
        self,
        client: BaseAIClient,
        environment: BaseExecutionEnvironment,
        workspace: BaseWorkspace,
        engineer_factory: Callable,
        project_parent: Optional[str] = None,
        max_retries: Optional[int] = 3,
        on_event: Optional[Callable] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
        provisioner: Optional["Provisioner"] = None,
        http_api_tester_factory: Optional[Callable] = None,
        integration_debugger_factory: Optional[Callable] = None,
        web_ui_tester_factory: Optional[Callable] = None,
    ):
        super().__init__(
            client=client,
            environment=environment,
            workspace=workspace,
            max_retries=max_retries,
            on_event=on_event,
            on_status_message=on_status_message,
        )
        self._engineer_factory = engineer_factory
        self._project_parent = project_parent
        self._provisioner = provisioner  # constructed lazily in build() if None
        self._http_api_tester_factory = http_api_tester_factory  # None → integration phase skipped
        self._integration_debugger_factory = integration_debugger_factory  # None → no auto-repair on integration failure
        self._web_ui_tester_factory = web_ui_tester_factory  # None → no Playwright UI tests

    @property
    def _process_system_prompt(self) -> str:
        return ARCHITECT_SYSTEM_PROMPT

    # ── Public API ─────────────────────────────────────────────────────────────

    def _models_snapshot(self) -> dict:
        """Best-effort snapshot of the model configuration for the run report.

        Reads from BiznizConfig if it loads cleanly; otherwise returns
        whatever we can pull off the architect's own AI client.
        """
        snap: dict = {}
        try:
            from bizniz.config.bizniz_config import BiznizConfig
            cfg = BiznizConfig.find_and_load()
            for key in (
                "default_model", "architect_model", "engineer_model",
                "coder_model", "tester_model", "debugger_model",
                "agentic_debugger_model", "planner_model",
            ):
                v = getattr(cfg, key, None)
                if v:
                    snap[key] = v
        except Exception:
            pass
        if not snap:
            try:
                snap["architect_client_model"] = getattr(
                    self._client, "_model", None,
                ) or getattr(self._client.ai_agent, "_model", None)
            except Exception:
                pass
        return {k: v for k, v in snap.items() if v is not None}

    def decompose(
        self, problem_statement: str, project_name: str,
    ) -> SystemArchitecture:
        """Decompose a problem statement into a service-based architecture."""

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        project_slug = slugify(project_name)

        log(f"Architect: decomposing '{project_name}' into services...")
        user_prompt = DECOMPOSE_PROMPT_TEMPLATE.format(
            problem_statement=problem_statement,
            project_name=project_name,
            project_slug=project_slug,
        )

        raw = self._call_ai_for_decomposition(user_prompt)

        services = [
            ServiceDefinition(**svc)
            for svc in raw.get("services", [])
        ]

        architecture = SystemArchitecture(
            project_name=raw["project_name"],
            project_slug=raw["project_slug"],
            services=services,
            docker_compose=raw.get("docker_compose"),  # optional preview
            description=raw["description"],
        )

        # Fresh decompose: every service is "new" by definition.
        for svc in architecture.services:
            svc.evolve_state = "new"

        log(
            f"Architect: architecture designed — "
            f"{len(architecture.services)} services: "
            f"{', '.join(s.name for s in architecture.services)}"
        )
        return architecture

    def evolve(
        self,
        milestone,
        existing_architecture: SystemArchitecture,
        problem_statement: str,
        project_name: str,
    ) -> SystemArchitecture:
        """
        Re-decompose a project for one milestone, preserving services that
        already exist.

        Parameters
        ----------
        milestone:
            The Milestone to deliver. Provides ``problem_slice``,
            ``use_cases``, ``success_criteria``, ``estimated_effort``.
        existing_architecture:
            The architecture as of before this milestone (services from
            prior milestones plus any infra). All of its services will
            appear in the returned architecture, tagged ``unchanged`` or
            ``extended``.
        problem_statement:
            The full project's problem statement (the milestone's slice
            references it).
        project_name:
            Human-readable project name.

        Returns
        -------
        ``SystemArchitecture`` whose ``services`` list is a superset of
        ``existing_architecture.services``: every existing service kept,
        plus any new services the milestone adds. Each service has
        ``evolve_state`` set.
        """
        from bizniz.architect.prompts.evolve_prompt import build_evolve_prompt
        from bizniz.architect.prompts.evolve_schema import EvolveArchitectSchema

        def log(msg: str) -> None:
            if self._on_status_message:
                self._on_status_message(msg)

        project_slug = slugify(project_name)

        existing_services_block = self._format_existing_services(existing_architecture)
        use_cases_block = "\n".join(
            f"    - {uc}" for uc in (milestone.use_cases or [])
        ) or "    (none specified)"
        success_block = "\n".join(
            f"    - {sc}" for sc in (milestone.success_criteria or [])
        ) or "    (none specified)"

        log(
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
        )

        raw = self._call_ai_for_evolve(user_prompt, EvolveArchitectSchema)

        existing_by_name = {s.name: s for s in existing_architecture.services}
        services: List[ServiceDefinition] = []
        for svc_raw in raw.get("services", []):
            state = svc_raw.get("evolve_state") or "unchanged"
            name = svc_raw["name"]
            # Preserve identity of existing services. The AI is told to
            # echo them back unchanged or extended; trust ID-bearing
            # fields from the existing architecture rather than
            # whatever the AI returns.
            if name in existing_by_name:
                prior = existing_by_name[name]
                # Keep the prior service's image_name (set after the
                # original build); allow new requirements/depends_on
                # to merge in via "extended".
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
                # Brand-new service.
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

        # If the AI dropped a previously-existing service (it shouldn't,
        # but defend anyway), put it back as "unchanged".
        returned_names = {s.name for s in services}
        for prior in existing_architecture.services:
            if prior.name not in returned_names:
                clone = prior.copy(deep=True) if hasattr(prior, "copy") else prior
                clone.evolve_state = "unchanged"
                services.append(clone)
                log(
                    f"Architect: evolve dropped service '{prior.name}' — "
                    f"restoring as unchanged"
                )

        evolved = SystemArchitecture(
            project_name=raw.get("project_name") or project_name,
            project_slug=raw.get("project_slug") or project_slug,
            services=services,
            docker_compose=None,
            description=raw.get("description") or existing_architecture.description,
        )

        new_count = sum(1 for s in evolved.services if s.evolve_state == "new")
        ext_count = sum(1 for s in evolved.services if s.evolve_state == "extended")
        log(
            f"Architect: milestone '{milestone.name}' → "
            f"{new_count} new + {ext_count} extended service(s) "
            f"(total {len(evolved.services)})"
        )
        return evolved

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

    def _call_ai_for_evolve(self, user_prompt: str, schema: dict) -> dict:
        """Single AI call for evolve(). Mirrors _call_ai_for_decomposition."""
        attempts = self.max_retries
        last_error = None

        def log(msg: str) -> None:
            if self._on_status_message:
                self._on_status_message(msg)

        self.clear_message_history()
        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                log(f"Architect: evolve AI call (attempt {attempt}/{attempts})...")
                t0 = time.time()
                text, _, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=schema,
                )
                elapsed = time.time() - t0
                log(f"Architect: evolve AI responded in {elapsed:.1f}s ({len(text or '')} chars)")
                self.add_messages_to_history(output_messages)
                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    continue
                text = self.clean_llm_json(text)
                return json.loads(text)
            except AIInsufficientFunds:
                raise
            except Exception as e:
                last_error = e
                log(f"Architect: evolve attempt {attempt} failed — {type(e).__name__}: {e}")
                continue

        raise ArchitectBadAIResponseError(
            f"AI failed to produce an evolved architecture after {attempts} attempts. "
            f"Last error: {last_error}"
        )

    # ── Director loop: Planner → Architect.evolve → Provisioner.evolve ─────

    def build_with_plan(
        self,
        problem_statement: str,
        project_name: str,
        planner=None,
        plan=None,
        parallel: bool = False,
        max_workers: int = 4,
        layered: bool = True,
        continue_on_failure: bool = False,
    ):
        """
        End-to-end milestone-driven build.

        Flow per project:
          1. Planner.plan(problem) → ProjectPlan with N milestones
          2. For each milestone in sequence_index order:
              a. Mark milestone in_progress
              b. tracker.set_milestone(milestone.db_id)
              c. Architect.evolve(milestone, current_architecture)
              d. Provisioner.evolve(evolved_architecture)
              e. Engineer dispatch on services flagged NEW or EXTENDED
              f. Mark milestone completed (or failed → stop, unless
                 continue_on_failure=True)
          3. Finalize job with rolled-up cost

        Parameters
        ----------
        planner:
            Optional pre-built Planner. If None, one is constructed from
            ``self._client`` (or a fresh client at the planner tier when
            available via the provisioner's parent).
        plan:
            Optional pre-computed ``ProjectPlan``. Skips the planner call.
        parallel / max_workers / layered:
            Forwarded to the engineer dispatch call (only relevant for
            services within a single milestone).
        continue_on_failure:
            If True, a failed milestone is marked failed but the next
            milestone still runs. Default False (stop at first failure).

        Returns
        -------
        A list of ``ArchitectResult``-like records, one per milestone.
        """
        from bizniz.architect.types import ArchitectResult
        from bizniz.cost import get_tracker
        from bizniz.planner import Planner
        from bizniz.project.project import Project
        from bizniz.provisioner import Provisioner
        from bizniz.workspace.local_workspace import LocalWorkspace

        def log(msg: str) -> None:
            if self._on_status_message:
                self._on_status_message(msg)

        tracker = get_tracker()
        provisional_slug = slugify(project_name)
        job_id = tracker.start_job(
            project_slug=provisional_slug,
            problem_statement=problem_statement,
        )
        log(f"Architect: build_with_plan opened cost job {job_id[:8]}…")
        job_status = "succeeded"

        provisioner = self._provisioner or Provisioner(
            project_parent=(
                Path(self._project_parent) if self._project_parent
                else self._workspace.root.parent
            ),
            on_status_message=self._on_status_message,
        )

        # Pre-flight: project root exists so we can attach the project DB
        # before the Planner runs (so plan + milestones land durably).
        parent = Path(self._project_parent) if self._project_parent else self._workspace.root.parent
        project = Project(root=parent / provisional_slug, project_name=project_name)
        project.create_structure()
        try:
            tracker.attach_project_db(project.db)
            project.db.start_job(
                job_id=job_id,
                project_slug=provisional_slug,
                problem_statement=problem_statement,
            )
        except Exception as e:
            log(f"Architect: cost tracker DB attach failed ({e})")

        results = []
        try:
            # Step 1: Plan (or use pre-supplied plan)
            if plan is None:
                tracker.set_phase("planner.plan")
                planner = planner or self._build_default_planner()
                plan = planner.plan(
                    problem_statement=problem_statement,
                    project_name=project_name,
                    project_db=project.db,
                )
                tracker.set_phase(None)
            log(f"Architect: walking {len(plan.milestones)} milestone(s)")

            # Walk milestones in sequence order (Planner ensures this is
            # a valid topological order over depends_on_names)
            current_architecture = SystemArchitecture(
                project_name=project_name,
                project_slug=provisional_slug,
                services=[],
                description="",
            )

            for milestone in sorted(plan.milestones, key=lambda m: m.sequence_index):
                m_label = f"#{milestone.sequence_index} '{milestone.name}'"
                log(f"Architect: ── milestone {m_label} ──")

                if milestone.db_id is not None:
                    project.db.update_milestone_status(milestone.db_id, "in_progress")
                    tracker.set_milestone(milestone.db_id)
                tracker.set_phase("architect.evolve")

                try:
                    # Step 2a: evolve architecture for this milestone
                    evolved_arch = self.evolve(
                        milestone=milestone,
                        existing_architecture=current_architecture,
                        problem_statement=problem_statement,
                        project_name=project_name,
                    )
                    new_count = sum(1 for s in evolved_arch.services if s.evolve_state == "new")
                    ext_count = sum(1 for s in evolved_arch.services if s.evolve_state == "extended")

                    # Step 2b: provision (idempotent)
                    tracker.set_phase("provisioner.evolve")
                    provision_result = provisioner.evolve(evolved_arch, project_name)

                    # Stamp image_name from provision back onto the arch
                    ps_by_name = {ps.name: ps for ps in provision_result.services}
                    for s in evolved_arch.services:
                        ps = ps_by_name.get(s.name)
                        if ps and ps.image_name:
                            s.image_name = ps.image_name

                    # Step 2b.5: validate the stack comes up healthy
                    # Keep the stack up if FusionAuth needs configuring
                    tracker.set_phase("stack_validation")
                    compose_path = str(project.dev_root / "docker-compose.yml")
                    has_auth = any(
                        s.service_type == "auth" for s in evolved_arch.services
                    )
                    stack_healthy = self._validate_and_repair_stack(
                        architecture=evolved_arch,
                        compose_path=compose_path,
                        project_root=project.root,
                        port_remap=provision_result.port_remap,
                        keep_up=has_auth,  # don't tear down if FusionAuth needs setup
                    )
                    if not stack_healthy:
                        log(f"Architect: milestone {m_label} — stack validation failed, skipping engineering")
                        if not continue_on_failure:
                            job_status = "failed"
                            results.append(ArchitectResult(
                                project_name=project_name,
                                architecture=evolved_arch,
                                service_results=[],
                                project_root=str(project.root),
                            ))
                            break
                    tracker.set_phase(None)

                    # Step 2b.6: configure FusionAuth (roles, test users, contract)
                    # Stack is still up from validation — FusionAuth is reachable
                    if has_auth and stack_healthy:
                        tracker.set_phase("fusionauth_provision")
                        try:
                            from bizniz.provisioner.fusionauth_agent import provision_fusionauth
                            # Read FusionAuth connection details from the .env
                            env_path = project.dev_root / ".env"
                            env_vars = {}
                            if env_path.exists():
                                for line in env_path.read_text().splitlines():
                                    line = line.strip()
                                    if line and not line.startswith("#") and "=" in line:
                                        k, v = line.split("=", 1)
                                        env_vars[k.strip()] = v.strip()

                            fa_host_url = env_vars.get("FUSIONAUTH_HOST_URL", "http://localhost:9011")
                            fa_api_key = env_vars.get("FUSIONAUTH_API_KEY", "")
                            fa_app_id = env_vars.get("FUSIONAUTH_APPLICATION_ID", "")
                            frontend_svc = next(
                                (s for s in evolved_arch.services if s.service_type == "frontend"),
                                None,
                            )
                            fe_port = frontend_svc.port if frontend_svc else 5173

                            fa_result = provision_fusionauth(
                                problem_statement=problem_statement,
                                project_root=project.root,
                                fusionauth_url=fa_host_url,
                                fusionauth_api_key=fa_api_key,
                                application_id=fa_app_id,
                                frontend_port=fe_port,
                                ai_client=self._client,
                                on_status=self._on_status_message,
                            )
                            log(
                                f"Architect: FusionAuth configured — "
                                f"{len(fa_result['roles'])} role(s), "
                                f"{len(fa_result['test_users'])} test user(s), "
                                f"smoke={'PASS' if fa_result['smoke_passed'] else 'FAIL'}"
                            )
                        except Exception as e:
                            log(f"Architect: FusionAuth provisioning failed ({e}) — continuing")
                        tracker.set_phase(None)

                    # Tear down stack after FusionAuth setup (or after validation
                    # if no auth service). Clean state for engineering.
                    if has_auth and stack_healthy:
                        from bizniz.provisioner.stack_validator import teardown_stack
                        teardown_stack(compose_path, self._on_status_message)

                    # Step 2c: engineer dispatch on changed services only
                    changed_services = [
                        s for s in evolved_arch.services
                        if s.evolve_state in ("new", "extended")
                        and s.service_type in {"backend", "frontend", "worker"}
                    ]
                    if not changed_services:
                        log(f"Architect: milestone {m_label} added no app services — skipping engineer dispatch")
                        m_results = []
                    else:
                        log(
                            f"Architect: dispatching engineers for "
                            f"{len(changed_services)} changed service(s): "
                            f"{', '.join(s.name for s in changed_services)}"
                        )
                        m_results = self._dispatch_engineers_for_milestone(
                            milestone=milestone,
                            changed_services=changed_services,
                            architecture=evolved_arch,
                            problem_statement=problem_statement,
                            project=project,
                            parallel=parallel,
                            max_workers=max_workers,
                            layered=layered,
                        )

                    milestone_succeeded = all(
                        getattr(r, "success", False) for r in m_results
                    ) if m_results else True

                    # Rebuild Docker images after engineering so the
                    # containers reflect the final code + dependencies
                    # (not the skeleton's initial state).
                    if milestone_succeeded and m_results:
                        tracker.set_phase("image_rebuild")
                        compose_path = str(project.dev_root / "docker-compose.yml")
                        app_svc_names = [
                            s.name for s in changed_services
                            if s.service_type in {"backend", "frontend", "worker"}
                        ]
                        if app_svc_names:
                            log(f"Architect: rebuilding images for {', '.join(app_svc_names)}...")
                            try:
                                import subprocess
                                subprocess.run(
                                    ["docker", "compose", "-f", compose_path, "build"] + app_svc_names,
                                    capture_output=True, text=True, timeout=300,
                                )
                            except Exception as e:
                                log(f"Architect: image rebuild failed ({e}) — integration may use stale images")
                        tracker.set_phase(None)

                    # Integration phase: run integration tests against the
                    # live stack after engineering passes. Same logic as
                    # build() — tests are the source of truth.
                    if (
                        milestone_succeeded
                        and m_results
                        and self._http_api_tester_factory is not None
                    ):
                        from bizniz.integration import run_integration_phase
                        from bizniz.workspace.local_workspace import LocalWorkspace as _LW
                        tracker.set_phase("integration")
                        # Build workspace map for ALL app services (not just
                        # changed ones) — integration tests verify the whole stack.
                        all_app_services = [
                            s for s in evolved_arch.services
                            if s.service_type in {"backend", "frontend", "worker"}
                        ]
                        all_workspaces = {
                            s.name: _LW(root=str(project.root / s.workspace_name))
                            for s in all_app_services
                            if (project.root / s.workspace_name).is_dir()
                        }
                        try:
                            # Use the milestone's problem_slice so integration
                            # tests only verify this milestone's scope, not the
                            # full project. M1 tests auth, not M3's rent collection.
                            milestone_problem = milestone.problem_slice or problem_statement
                            log(f"Architect: milestone {m_label} — running integration phase...")
                            m_results = run_integration_phase(
                                architecture=evolved_arch,
                                service_results=list(m_results),
                                project_root=project.root,
                                problem_statement=milestone_problem,
                                compose_path=compose_path,
                                http_api_tester_factory=self._http_api_tester_factory,
                                service_workspaces=all_workspaces,
                                on_status=self._on_status_message,
                                debugger_factory=self._integration_debugger_factory,
                                web_ui_tester_factory=self._web_ui_tester_factory,
                            )
                            # Re-evaluate success after integration
                            milestone_succeeded = all(
                                getattr(r, "success", False) for r in m_results
                            )
                        except Exception as e:
                            log(f"Architect: milestone {m_label} integration phase raised ({e}) — continuing")
                        tracker.set_phase(None)

                    if milestone_succeeded:
                        if milestone.db_id is not None:
                            project.db.update_milestone_status(milestone.db_id, "completed")
                        log(f"Architect: milestone {m_label} ✓ completed (new={new_count}, extended={ext_count})")
                    else:
                        log(f"Architect: milestone {m_label} ✗ failed (some services didn't pass)")
                        try:
                            project.db.log_build_event(
                                "_milestone_", "image_build", False,
                                f"Milestone {milestone.name} failed",
                            )
                        except Exception:
                            pass
                        if not continue_on_failure:
                            job_status = "failed"
                            results.append(ArchitectResult(
                                project_name=project_name,
                                architecture=evolved_arch,
                                service_results=list(m_results),
                                project_root=str(project.root),
                            ))
                            break

                    # Update current_architecture so the next milestone's
                    # evolve sees what's there now.
                    current_architecture = evolved_arch

                    results.append(ArchitectResult(
                        project_name=project_name,
                        architecture=evolved_arch,
                        service_results=list(m_results),
                        project_root=str(project.root),
                    ))

                except AIInsufficientFunds:
                    raise
                except Exception as e:
                    log(f"Architect: milestone {m_label} crashed: {type(e).__name__}: {e}")
                    job_status = "failed"
                    if not continue_on_failure:
                        raise
                finally:
                    tracker.set_milestone(None)
                    tracker.set_phase(None)

            # Persist final-state architecture docs
            _save_architecture_docs(project.root, current_architecture)
            return results

        finally:
            try:
                tracker.finish_job(status=job_status)
                summary = tracker.summary()
                log(
                    f"Architect: build_with_plan {job_status} — "
                    f"calls={summary.calls} cost=${summary.total_cost:.4f}"
                )
            except Exception as e:
                log(f"Architect: cost job finish failed ({e})")

    def _build_default_planner(self):
        """Construct a Planner using the architect's own client. Used as
        a fallback when build_with_plan() is called without one."""
        from bizniz.planner import Planner
        return Planner(
            client=self._client,
            environment=self._environment,
            workspace=self._workspace,
            on_status_message=self._on_status_message,
        )

    def _dispatch_engineers_for_milestone(
        self,
        milestone,
        changed_services,
        architecture: SystemArchitecture,
        problem_statement: str,
        project,
        parallel: bool,
        max_workers: int,
        layered: bool,
    ):
        """Same as the layered/parallel dispatch in ``build()``, but
        scoped to ``changed_services`` (only NEW or EXTENDED services
        for the current milestone). Engineers analyze using the
        milestone's ``problem_slice`` so issue lists stay milestone-scoped.
        """
        log = self._on_status_message or (lambda _msg: None)

        from bizniz.workspace.local_workspace import LocalWorkspace
        service_workspaces = {
            s.name: LocalWorkspace(root=str(project.root / s.workspace_name))
            for s in changed_services
        }

        # The milestone's problem_slice replaces the project-wide problem
        # statement for this engineer dispatch — keeps the issue list
        # scoped to what this milestone delivers.
        milestone_problem = milestone.problem_slice or problem_statement

        layers = _sort_services_by_dependency(changed_services)
        results = []
        for layer in layers:
            if parallel and len(layer) > 1:
                layer_results = self._dispatch_engineers_parallel(
                    layer, service_workspaces, milestone_problem,
                    architecture, project, max_workers, layered,
                )
            else:
                layer_results = self._dispatch_engineers_sequential(
                    layer, service_workspaces, milestone_problem,
                    architecture, project, layered,
                )
            results.extend(layer_results)
        return results

    def build(
        self, problem_statement: str, project_name: str,
        parallel: bool = True, max_workers: int = 4,
        layered: bool = True,
        force_no_skeleton: bool = False,
    ) -> ArchitectResult:
        """
        Full pipeline:
          1. Decompose problem into services (this class)
          2. Provision the project on disk (Provisioner: directory tree,
             skeleton seeding / app templates, infra templates, compose,
             .env, Docker images)
          3. Dispatch Engineer for each application service (this class)

        The architect plans; the Provisioner materializes; the engineer
        codes. Architect.build() is the thin orchestration shell.

        ``force_no_skeleton``: after decompose, override every app
        service's ``skeleton`` to ``"none"`` so the Provisioner falls
        back to the minimal generated boilerplate (Dockerfile +
        requirements.txt / package.json) and the AI must build the rest
        from scratch. Used for apples-to-apples cost experiments.
        """
        from bizniz.project.project import Project
        from bizniz.provisioner import Provisioner
        from bizniz.cost import get_tracker
        from bizniz.workspace.naming import slugify

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Open a cost-tracker job up-front so the architect's own AI calls
        # (decompose, plan-architecture during engineer.analyze, etc.) get
        # tagged with a job_id even before the project DB exists. The DB is
        # attached after the Provisioner creates the project; buffered
        # records flush at that point.
        tracker = get_tracker()
        provisional_slug = slugify(project_name)
        run_started_at = datetime.datetime.now(datetime.timezone.utc)
        job_id = tracker.start_job(
            project_slug=provisional_slug,
            problem_statement=problem_statement,
        )
        tracker.set_phase("architect.decompose")
        log(f"Architect: cost job {job_id[:8]}… opened for '{project_name}'")
        job_status = "succeeded"

        # Captured during the try-block so the finally-block report can
        # still write something useful when an early step raises.
        _captured_architecture = None
        _captured_service_results: list = []
        _captured_project_root: Optional[Path] = None
        _captured_compose_path: Optional[str] = None

        try:
            # Step 1: Decompose
            architecture = self.decompose(problem_statement, project_name)
            _captured_architecture = architecture
            tracker.set_phase(None)

            if force_no_skeleton:
                _APP_TYPES = {"backend", "frontend", "worker"}
                wiped = []
                for svc in architecture.services:
                    if svc.service_type in _APP_TYPES and svc.skeleton and svc.skeleton != "none":
                        wiped.append(f"{svc.name}({svc.skeleton})")
                        svc.skeleton = "none"
                if wiped:
                    log(f"Architect: --no-skeleton — wiped skeletons on {', '.join(wiped)}")

            # Step 2: Provisioner — turn the plan into a real project on disk +
            # built Docker images.
            provisioner = self._provisioner or Provisioner(
                project_parent=(
                    Path(self._project_parent) if self._project_parent
                    else self._workspace.root.parent
                ),
                on_status_message=self._on_status_message,
            )
            provision_result = provisioner.provision(architecture, project_name)

            project = Project(
                root=Path(provision_result.project_root),
                project_name=project_name,
            )
            _captured_project_root = Path(provision_result.project_root)

            # Wire the project DB into the cost tracker. This flushes any
            # records buffered before the project existed (architect's
            # decompose call) and live-persists everything that follows.
            try:
                tracker.attach_project_db(project.db)
                project.db.start_job(
                    job_id=job_id,
                    project_slug=architecture.project_slug,
                    problem_statement=problem_statement,
                )
            except Exception as e:
                log(f"Architect: cost tracker DB attach failed ({e}) — continuing in-memory only")

            # Save human-readable architecture docs (provisioner already saved
            # the architecture snapshot to project DB).
            _save_architecture_docs(project.root, architecture)

            # Build a workspace map for engineer dispatch from the provision result.
            from bizniz.workspace.local_workspace import LocalWorkspace
            service_workspaces = {}
            for ps in provision_result.services:
                if ps.is_infrastructure or ps.workspace_path is None:
                    continue
                service_workspaces[ps.name] = LocalWorkspace(root=ps.workspace_path)

            # Stamp image_name back onto ServiceDefinitions so engineer dispatch
            # can pass the right image to the test environment.
            ps_by_name = {ps.name: ps for ps in provision_result.services}
            for service in architecture.services:
                ps = ps_by_name.get(service.name)
                if ps and ps.image_name:
                    service.image_name = ps.image_name

            # Step 2.5: Validate the stack comes up healthy before engineering
            compose_path = str(project.dev_root / "docker-compose.yml")
            _captured_compose_path = compose_path
            tracker.set_phase("stack_validation")
            stack_healthy = self._validate_and_repair_stack(
                architecture=architecture,
                compose_path=compose_path,
                project_root=project.root,
                port_remap=provision_result.port_remap,
            )
            if not stack_healthy:
                log("Architect: stack validation failed — continuing with engineering (may fail at integration)")
            tracker.set_phase(None)

            # Step 3: Dispatch engineers for application services (in dependency order)
            app_services = [s for s in architecture.services if s.name in service_workspaces]
            service_layers = _sort_services_by_dependency(app_services)
            log(f"Architect: {len(app_services)} services in {len(service_layers)} dependency layer(s)")

            self._captured_contracts: Dict[str, dict] = {}
            service_results = []
            for layer_idx, layer in enumerate(service_layers):
                layer_names = [s.name for s in layer]
                log(f"Architect: dispatching layer {layer_idx + 1} ({', '.join(layer_names)})...")
                if parallel and len(layer) > 1:
                    layer_results = self._dispatch_engineers_parallel(
                        layer, service_workspaces, problem_statement, architecture, project, max_workers, layered,
                    )
                else:
                    layer_results = self._dispatch_engineers_sequential(
                        layer, service_workspaces, problem_statement, architecture, project, layered,
                    )
                service_results.extend(layer_results)
                _captured_service_results = list(service_results)

                # If this layer produced any HTTP backends that passed
                # AND there's another layer coming, capture their
                # OpenAPI specs so the next layer's engineers see
                # actual endpoints, not guesses. Skipped on the last
                # layer (the integration phase below captures all).
                if layer_idx < len(service_layers) - 1:
                    layer_passed_backends = [
                        s.name for s in layer
                        if s.service_type == "backend" and s.port and any(
                            r.service_name == s.name and r.success
                            for r in layer_results
                        )
                    ]
                    if layer_passed_backends:
                        from bizniz.integration.contracts import capture_backend_contracts
                        compose_path_so_far = str(project.dev_root / "docker-compose.yml")
                        log(
                            f"Architect: capturing contracts from layer {layer_idx + 1} "
                            f"backends ({', '.join(layer_passed_backends)}) for downstream layers..."
                        )
                        try:
                            captured = capture_backend_contracts(
                                architecture=architecture,
                                project_root=project.root,
                                compose_path=compose_path_so_far,
                                on_status=self._on_status_message,
                                only_names=layer_passed_backends,
                            )
                            self._captured_contracts.update(captured)
                        except Exception as e:
                            log(f"Architect: between-layer contract capture failed ({e}) — continuing without contracts")

            compose_path = str(project.dev_root / "docker-compose.yml")
            _captured_compose_path = compose_path

            # Rebuild images after engineering so containers have the
            # final code + dependencies (not the skeleton's initial state).
            if service_results and any(getattr(r, "success", False) for r in service_results):
                tracker.set_phase("image_rebuild")
                rebuild_names = [
                    s.name for s in app_services
                    if any(r.service_name == s.name and r.success for r in service_results)
                ]
                if rebuild_names:
                    log(f"Architect: rebuilding images for {', '.join(rebuild_names)}...")
                    try:
                        import subprocess as _sp
                        _sp.run(
                            ["docker", "compose", "-f", compose_path, "build"] + rebuild_names,
                            capture_output=True, text=True, timeout=300,
                        )
                    except Exception as e:
                        log(f"Architect: image rebuild failed ({e}) — integration may use stale images")
                tracker.set_phase(None)

            # Post-build integration phase: bring the stack up, capture
            # backend contracts, dispatch HTTPApiTester for each
            # backend to author + run real integration tests, fail any
            # service whose tests don't pass. Replaces the framework-
            # coupled smoke_verification with a generative tester that
            # works across any HTTP backend skeleton. Skipped if no
            # services passed engineering OR no tester factory was
            # provided.
            if (
                self._http_api_tester_factory is not None
                and service_results
                and any(getattr(r, "success", False) for r in service_results)
            ):
                from bizniz.integration import run_integration_phase
                tracker.set_phase("integration")
                try:
                    service_results = run_integration_phase(
                        architecture=architecture,
                        service_results=service_results,
                        project_root=project.root,
                        problem_statement=problem_statement,
                        compose_path=compose_path,
                        http_api_tester_factory=self._http_api_tester_factory,
                        service_workspaces=service_workspaces,
                        on_status=self._on_status_message,
                        debugger_factory=self._integration_debugger_factory,
                        web_ui_tester_factory=self._web_ui_tester_factory,
                    )
                    _captured_service_results = list(service_results)
                except Exception as e:
                    log(f"Architect: integration phase raised ({e}) — continuing without it")
                tracker.set_phase(None)

            # Determine job status from service results
            if service_results and not all(getattr(r, "success", False) for r in service_results):
                job_status = "failed"
            return ArchitectResult(
                project_name=project_name,
                architecture=architecture,
                service_results=service_results,
                docker_compose_path=compose_path,
                project_root=str(project.root),
            )
        except Exception:
            job_status = "failed"
            raise
        finally:
            tracker.set_service(None)
            tracker.set_issue(None)
            tracker.set_phase(None)
            try:
                tracker.finish_job(status=job_status)
                summary = tracker.summary()
                log(
                    f"Architect: cost job {job_id[:8]}… {job_status} — "
                    f"calls={summary.calls} cost=${summary.total_cost:.4f}"
                )
            except Exception as e:
                log(f"Architect: cost job finish failed ({e})")
                summary = None

            # Per-run efficiency doc — best-effort. Skip when there's no
            # project root (provisioner step never ran). Failures here
            # never crash the build.
            if _captured_project_root is not None:
                try:
                    from bizniz.run_report import write_run_report
                    md_path = write_run_report(
                        project_name=project_name,
                        project_slug=(
                            _captured_architecture.project_slug
                            if _captured_architecture else provisional_slug
                        ),
                        project_root=_captured_project_root,
                        job_id=job_id,
                        started_at=run_started_at,
                        finished_at=datetime.datetime.now(datetime.timezone.utc),
                        status=job_status,
                        architecture=_captured_architecture,
                        service_results=_captured_service_results,
                        cost_summary=summary if summary is not None else tracker.summary(),
                        models=self._models_snapshot(),
                        docker_compose_path=_captured_compose_path,
                    )
                    log(f"Architect: run report written to {md_path}")
                except Exception as e:
                    log(f"Architect: run report write failed ({e}) — continuing")

    # ── Stack validation ────────────────────────────────────────────────────────

    def _validate_and_repair_stack(
        self,
        architecture: SystemArchitecture,
        compose_path: str,
        project_root: Path,
        port_remap: Optional[Dict] = None,
        max_repair_iterations: int = 3,
        keep_up: bool = False,
    ) -> bool:
        """Bring the stack up, health-check, and auto-repair on failure.

        Returns True if the stack is healthy (possibly after repairs).
        If unhealthy after all iterations, logs the failure and returns False
        — the caller can decide whether to abort or continue.
        """
        from bizniz.provisioner.stack_validator import validate_stack
        from bizniz.integration.debug_loop import repair_integration_failure

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        validation = validate_stack(
            architecture=architecture,
            compose_path=compose_path,
            on_status=self._on_status_message,
            port_remap=port_remap,
            teardown=not keep_up,
        )

        if validation.healthy:
            return True

        # Stack is unhealthy — try to repair infrastructure files
        if self._integration_debugger_factory is None:
            log("Architect: stack unhealthy but no debugger factory — cannot auto-repair")
            log(f"Architect: unhealthy services: {[s.name for s in validation.unhealthy_services]}")
            return False

        failure_output = validation.failure_summary()
        log(f"Architect: stack unhealthy — {len(validation.unhealthy_services)} service(s) failed. Dispatching infra debugger...")

        # Build a workspace rooted at the project for infra file access
        from bizniz.workspace.local_workspace import LocalWorkspace
        infra_workspace = LocalWorkspace(root=str(project_root), create=False)

        def _debugger_factory():
            return self._integration_debugger_factory(infra_workspace)

        def _rerun_stack():
            """Re-validate the stack after a repair attempt."""
            revalidation = validate_stack(
                architecture=architecture,
                compose_path=compose_path,
                on_status=self._on_status_message,
                port_remap=port_remap,
                service_timeout_s=45.0,
            )
            if revalidation.healthy:
                return True, "Stack is healthy"
            return False, revalidation.failure_summary()

        def _capture_infra_logs():
            """Capture logs from all unhealthy services."""
            parts = []
            for svc in validation.unhealthy_services:
                from bizniz.provisioner.stack_validator import _capture_logs
                logs = _capture_logs(compose_path, svc.name)
                if logs.strip():
                    parts.append(f"=== {svc.name} ===\n{logs}")
            return "\n\n".join(parts)

        # Use the first unhealthy service as the "service" for the debug loop
        # (the debugger can edit any file in the project workspace)
        from bizniz.architect.types import ServiceDefinition as _SD
        infra_service = _SD(
            name="infrastructure",
            service_type="backend",
            framework="docker",
            language="yaml",
            description="Docker infrastructure (Dockerfile, compose, init scripts)",
            workspace_name=".",
        )

        repaired, final_output = repair_integration_failure(
            service=infra_service,
            workspace=infra_workspace,
            failure_output=failure_output,
            integration_test_rel="(infrastructure stack validation)",
            debugger_factory=_debugger_factory,
            rerun_tests=_rerun_stack,
            on_status=self._on_status_message,
            max_iterations=max_repair_iterations,
            capture_logs=_capture_infra_logs,
            compose_path=compose_path,
        )

        if repaired:
            log("Architect: stack repaired — infrastructure is healthy")
            # Rebuild images if Dockerfiles were modified
            try:
                import subprocess
                subprocess.run(
                    ["docker", "compose", "-f", compose_path, "build"],
                    capture_output=True, text=True, timeout=300,
                )
            except Exception:
                pass
        else:
            log("Architect: stack repair failed — infrastructure still unhealthy")
            log(f"Architect: last failure:\n{final_output[-500:] if final_output else '(no output)'}")

        return repaired

    # ── Private helpers ────────────────────────────────────────────────────────

    def _call_ai_for_decomposition(self, user_prompt: str) -> dict:
        """Call AI for system decomposition and return parsed JSON."""
        attempts = self.max_retries
        last_error = None

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        self.clear_message_history()
        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                log(f"Architect: AI decomposition call (attempt {attempt}/{attempts})...")
                t0 = time.time()
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=ArchitectSchema,
                )
                elapsed = time.time() - t0
                log(f"Architect: AI responded in {elapsed:.1f}s ({len(text or '')} chars)")
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    log(f"Architect: empty response on attempt {attempt}")
                    continue

                text = self.clean_llm_json(text)
                return json.loads(text)

            except AIInsufficientFunds:
                raise
            except Exception as e:
                last_error = e
                log(f"Architect: attempt {attempt} failed — {type(e).__name__}: {e}")
                continue

        raise ArchitectBadAIResponseError(
            f"AI failed to produce system architecture after {attempts} attempts. "
            f"Last error: {last_error}"
        )

    def _dispatch_engineers_sequential(
        self,
        app_services,
        service_workspaces,
        problem_statement,
        architecture,
        project,
        layered: bool = True,
    ) -> List[ServiceResult]:
        """Dispatch engineers for services one at a time."""

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        service_results = []
        for service in app_services:
            workspace = service_workspaces[service.name]
            service_prompt = self._build_service_prompt(
                problem_statement, service, architecture,
            )
            log(f"Architect: engineering service '{service.name}' ({service.framework}/{service.language})...")

            try:
                result = self._dispatch_engineer(
                    workspace=workspace,
                    service=service,
                    service_prompt=service_prompt,
                    project=project,
                    layered=layered,
                )
                service_results.append(result)
                status = "PASS" if result.success else "FAIL"
                log(
                    f"Architect: service '{service.name}' — {status} "
                    f"({result.issues_passed}/{result.issues_total} issues)"
                )
            except AIInsufficientFunds:
                log("Architect: API account has insufficient funds — stopping.")
                raise
            except Exception as e:
                log(f"Architect: service '{service.name}' failed — {type(e).__name__}: {e}")
                service_results.append(ServiceResult(
                    service_name=service.name,
                    workspace_name=service.workspace_name,
                    success=False,
                    error=str(e),
                ))
        return service_results

    def _dispatch_engineers_parallel(
        self,
        app_services,
        service_workspaces,
        problem_statement,
        architecture,
        project,
        max_workers: int,
        layered: bool = True,
    ) -> List[ServiceResult]:
        """Dispatch engineers for all application services in parallel."""
        project_db_lock = threading.Lock()

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        service_results = []
        futures = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            for service in app_services:
                workspace = service_workspaces[service.name]
                service_prompt = self._build_service_prompt(
                    problem_statement, service, architecture,
                )
                log(f"Architect: submitting service '{service.name}' to thread pool...")
                future = executor.submit(
                    self._dispatch_engineer,
                    workspace=workspace,
                    service=service,
                    service_prompt=service_prompt,
                    project=project,
                    project_db_lock=project_db_lock,
                    layered=layered,
                )
                futures[future] = service

            for future in concurrent.futures.as_completed(futures):
                service = futures[future]
                try:
                    result = future.result()
                    service_results.append(result)
                    status = "PASS" if result.success else "FAIL"
                    log(
                        f"Architect: service '{service.name}' — {status} "
                        f"({result.issues_passed}/{result.issues_total} issues)"
                    )
                except AIInsufficientFunds:
                    log("Architect: API account has insufficient funds — stopping.")
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as e:
                    log(f"Architect: service '{service.name}' failed — {type(e).__name__}: {e}")
                    service_results.append(ServiceResult(
                        service_name=service.name,
                        workspace_name=service.workspace_name,
                        success=False,
                        error=str(e),
                    ))

        return service_results

    def _dispatch_engineer(
        self,
        workspace,
        service: ServiceDefinition,
        service_prompt: str,
        project=None,
        project_db_lock=None,
        layered: bool = True,
    ) -> ServiceResult:
        """Dispatch an Engineer for a single service."""
        # Tag the cost tracker with the current service so every AI call
        # made inside this dispatch attributes to the right service in the
        # api_calls rollup.
        from bizniz.cost import get_tracker
        tracker = get_tracker()
        tracker.set_service(service.name)
        tracker.set_phase("engineer.analyze")
        with self._engineer_factory(
            workspace,
            on_status_message=self._on_status_message,
            image_name=service.image_name,
            language=service.language,
        ) as engineer:
            if layered:
                # Layered generation: batch issues by dependency layer
                analysis = engineer.analyze(service_prompt)

                # Log issues to project DB
                if project:
                    for issue in analysis.issues:
                        if project_db_lock:
                            with project_db_lock:
                                project.db.log_issue(
                                    service_name=service.name,
                                    title=issue.title,
                                    description=issue.description,
                                )
                        else:
                            project.db.log_issue(
                                service_name=service.name,
                                title=issue.title,
                                description=issue.description,
                            )

                # Three-phase strategy: cheap framing pass → escalation chain
                # over all still-failing tickets → agentic debug on what's left.
                results = engineer.run_three_phase(service_prompt, analysis=analysis)
            else:
                # Sequential per-issue dispatch (legacy behavior)
                analysis = engineer.analyze(service_prompt)
                results = []
                for issue in analysis.issues:
                    issue_db_id = None
                    if project:
                        if project_db_lock:
                            with project_db_lock:
                                issue_db_id = project.db.log_issue(
                                    service_name=service.name,
                                    title=issue.title,
                                    description=issue.description,
                                )
                        else:
                            issue_db_id = project.db.log_issue(
                                service_name=service.name,
                                title=issue.title,
                                description=issue.description,
                            )

                    try:
                        result = engineer.dispatch(issue.db_id)
                        results.append(result)

                        if project and issue_db_id:
                            status = "closed" if result.success else "failed"
                            if project_db_lock:
                                with project_db_lock:
                                    project.db.update_issue(
                                        issue_db_id, status,
                                        strategy_used=getattr(result, 'strategy_used', None),
                                        iterations=result.iterations,
                                    )
                            else:
                                project.db.update_issue(
                                    issue_db_id, status,
                                    strategy_used=getattr(result, 'strategy_used', None),
                                    iterations=result.iterations,
                                )
                    except AIInsufficientFunds:
                        raise
                    except Exception as e:
                        results.append(type('R', (), {
                            'success': False, 'iterations': 0,
                        })())
                        if project and issue_db_id:
                            if project_db_lock:
                                with project_db_lock:
                                    project.db.update_issue(issue_db_id, "failed")
                            else:
                                project.db.update_issue(issue_db_id, "failed")

        successes = sum(1 for r in results if getattr(r, 'success', False))
        total = len(results)
        return ServiceResult(
            service_name=service.name,
            workspace_name=service.workspace_name,
            success=successes == total and total > 0,
            issues_total=total,
            issues_passed=successes,
        )

    def _build_service_prompt(
        self,
        problem_statement: str,
        service: ServiceDefinition,
        architecture: SystemArchitecture,
    ) -> str:
        """Build a focused prompt for a single service."""
        other_services = [
            f"- {s.name} ({s.framework}): {s.description}"
            for s in architecture.services
            if s.name != service.name
        ]
        other_services_text = "\n".join(other_services) if other_services else "(none)"

        # Backend contracts captured from prior layers — present only
        # for services dispatched after at least one backend has been
        # built and verified. Frontends use this to know exactly which
        # endpoints exist and what shapes they accept, so they don't
        # have to guess.
        contracts_section = self._format_contracts_for_prompt(architecture, service)

        return (
            f"Overall project: {problem_statement}\n\n"
            f"You are building the '{service.name}' service for the "
            f"'{architecture.project_name}' project.\n\n"
            f"Service details:\n"
            f"- Type: {service.service_type}\n"
            f"- Framework: {service.framework}\n"
            f"- Language: {service.language}\n"
            f"- Description: {service.description}\n"
            f"- Port: {service.port}\n\n"
            f"Other services in the system:\n{other_services_text}\n\n"
            f"{contracts_section}"
            f"Build ONLY this service. Use {service.language} with {service.framework}. "
            f"Focus on clean, working code with tests. "
            f"The service will run in a Docker container."
        )

    def _format_contracts_for_prompt(
        self,
        architecture: SystemArchitecture,
        current_service: ServiceDefinition,
    ) -> str:
        """Render captured backend OpenAPI contracts as a prompt
        section. Returns an empty string when no contracts have been
        captured yet (first-layer services).

        For frontends, this turns "guess what the backend looks like"
        into "consume the spec the backend just published" — closing
        the contract drift that integration tests would otherwise
        have to catch reactively.
        """
        contracts = getattr(self, "_captured_contracts", None) or {}
        if not contracts:
            return ""
        lines = [
            "Backend contracts (already built and verified — call these "
            "endpoints, do not guess shapes):\n"
        ]
        for svc_name, doc in contracts.items():
            if svc_name == current_service.name:
                continue  # don't show a service its own contract
            svc_def = next(
                (s for s in architecture.services if s.name == svc_name), None,
            )
            base = f"http://{svc_name}:{svc_def.port}" if svc_def and svc_def.port else f"http://{svc_name}"
            lines.append(f"### {svc_name} — base URL: {base}")
            paths = doc.get("paths") or {}
            for path, ops in sorted(paths.items()):
                if not isinstance(ops, dict):
                    continue
                methods = sorted(m.upper() for m in ops.keys() if isinstance(ops.get(m), dict))
                if methods:
                    lines.append(f"  {','.join(methods):<20s} {path}")
            lines.append("")
        return "\n".join(lines) + "\n"


def _sort_services_by_dependency(services):
    """
    Sort services into dependency layers using topological sort.

    Services with no dependencies (or only infrastructure dependencies) go first.
    Services that depend on other app services go in later layers.
    Services within the same layer can be dispatched in parallel.

    Returns a list of layers, where each layer is a list of ServiceDefinition.
    """
    app_names = {s.name for s in services}
    service_map = {s.name: s for s in services}

    # Build adjacency: only track deps on other app services
    deps = {}
    for s in services:
        deps[s.name] = [d for d in s.depends_on if d in app_names]

    layers = []
    resolved = set()

    while len(resolved) < len(services):
        # Find services whose deps are all resolved
        layer = [
            name for name in deps
            if name not in resolved and all(d in resolved for d in deps[name])
        ]
        if not layer:
            # Circular dependency — dump remaining services into one layer
            layer = [name for name in deps if name not in resolved]
        layers.append([service_map[name] for name in layer])
        resolved.update(layer)

    return layers


def _save_architecture_docs(project_root: Path, architecture: SystemArchitecture):
    """Save a human-readable architecture overview to docs/architecture.md."""
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# {architecture.project_name} — Architecture",
        "",
        architecture.description,
        "",
        f"## Services ({len(architecture.services)})",
        "",
    ]

    for svc in architecture.services:
        lines.append(f"### {svc.name}")
        lines.append(f"- **Type:** {svc.service_type}")
        lines.append(f"- **Framework:** {svc.framework}")
        lines.append(f"- **Language:** {svc.language}")
        if svc.port:
            lines.append(f"- **Port:** {svc.port}")
        if svc.depends_on:
            lines.append(f"- **Depends on:** {', '.join(svc.depends_on)}")
        lines.append(f"- **Description:** {svc.description}")
        if svc.requirements:
            lines.append(f"- **Packages:** {', '.join(svc.requirements)}")
        lines.append("")

    if architecture.docker_compose:
        lines.append("## Docker Compose (AI preview)")
        lines.append("")
        lines.append(
            "_Note: this is the architect's compose preview; the actual "
            "compose used to run the project is generated deterministically "
            "by the Provisioner._"
        )
        lines.append("")
        lines.append("```yaml")
        lines.append(architecture.docker_compose)
        lines.append("```")
        lines.append("")

    (docs_dir / "architecture.md").write_text("\n".join(lines), encoding="utf-8")

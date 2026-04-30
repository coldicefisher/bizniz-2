"""
AutoArchitect

Takes a problem statement and project name, decomposes the system into
containerized services, creates the project directory structure, generates
Dockerfiles and docker-compose.yml, builds Docker images, and dispatches
AutoEngineer instances for application services.

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
import json
import threading
import time
from pathlib import Path
from typing import Optional, Callable, List, TYPE_CHECKING

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
    AutoArchitectBadAIResponseError,
)
from bizniz.architect.prompts.system_prompt import AUTO_ARCHITECT_SYSTEM_PROMPT
from bizniz.architect.prompts.decompose_prompt import DECOMPOSE_PROMPT_TEMPLATE
from bizniz.architect.prompts.schema import AutoArchitectSchema

if TYPE_CHECKING:
    from bizniz.provisioner import Provisioner


# Service types that are application code (need workspaces + engineers)
_APPLICATION_TYPES = {"backend", "frontend", "worker"}

# Service types that are infrastructure (use standard images, no workspace)
_INFRASTRUCTURE_TYPES = {"database", "cache", "proxy", "auth"}


class AutoArchitect(BaseAIAgent):
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
        Callable(workspace, on_status_message, image_name) → AutoEngineer context manager.
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

    @property
    def _process_system_prompt(self) -> str:
        return AUTO_ARCHITECT_SYSTEM_PROMPT

    # ── Public API ─────────────────────────────────────────────────────────────

    def decompose(
        self, problem_statement: str, project_name: str,
    ) -> SystemArchitecture:
        """Decompose a problem statement into a service-based architecture."""

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        project_slug = slugify(project_name)

        log(f"AutoArchitect: decomposing '{project_name}' into services...")
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

        log(
            f"AutoArchitect: architecture designed — "
            f"{len(architecture.services)} services: "
            f"{', '.join(s.name for s in architecture.services)}"
        )
        return architecture

    def build(
        self, problem_statement: str, project_name: str,
        parallel: bool = True, max_workers: int = 4,
        layered: bool = True,
    ) -> ArchitectResult:
        """
        Full pipeline:
          1. Decompose problem into services (this class)
          2. Provision the project on disk (Provisioner: directory tree,
             skeleton seeding / app templates, infra templates, compose,
             .env, Docker images)
          3. Dispatch AutoEngineer for each application service (this class)

        The architect plans; the Provisioner materializes; the engineer
        codes. Architect.build() is the thin orchestration shell.
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
        job_id = tracker.start_job(
            project_slug=provisional_slug,
            problem_statement=problem_statement,
        )
        tracker.set_phase("architect.decompose")
        log(f"AutoArchitect: cost job {job_id[:8]}… opened for '{project_name}'")
        job_status = "succeeded"

        try:
            # Step 1: Decompose
            architecture = self.decompose(problem_statement, project_name)
            tracker.set_phase(None)

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
                log(f"AutoArchitect: cost tracker DB attach failed ({e}) — continuing in-memory only")

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

            # Step 3: Dispatch engineers for application services (in dependency order)
            app_services = [s for s in architecture.services if s.name in service_workspaces]
            service_layers = _sort_services_by_dependency(app_services)
            log(f"AutoArchitect: {len(app_services)} services in {len(service_layers)} dependency layer(s)")

            service_results = []
            for layer_idx, layer in enumerate(service_layers):
                layer_names = [s.name for s in layer]
                log(f"AutoArchitect: dispatching layer {layer_idx + 1} ({', '.join(layer_names)})...")
                if parallel and len(layer) > 1:
                    layer_results = self._dispatch_engineers_parallel(
                        layer, service_workspaces, problem_statement, architecture, project, max_workers, layered,
                    )
                else:
                    layer_results = self._dispatch_engineers_sequential(
                        layer, service_workspaces, problem_statement, architecture, project, layered,
                    )
                service_results.extend(layer_results)

            compose_path = str(project.dev_root / "docker-compose.yml")
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
                    f"AutoArchitect: cost job {job_id[:8]}… {job_status} — "
                    f"calls={summary.calls} cost=${summary.total_cost:.4f}"
                )
            except Exception as e:
                log(f"AutoArchitect: cost job finish failed ({e})")

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
                log(f"AutoArchitect: AI decomposition call (attempt {attempt}/{attempts})...")
                t0 = time.time()
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=AutoArchitectSchema,
                )
                elapsed = time.time() - t0
                log(f"AutoArchitect: AI responded in {elapsed:.1f}s ({len(text or '')} chars)")
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    log(f"AutoArchitect: empty response on attempt {attempt}")
                    continue

                text = self.clean_llm_json(text)
                return json.loads(text)

            except AIInsufficientFunds:
                raise
            except Exception as e:
                last_error = e
                log(f"AutoArchitect: attempt {attempt} failed — {type(e).__name__}: {e}")
                continue

        raise AutoArchitectBadAIResponseError(
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
            log(f"AutoArchitect: engineering service '{service.name}' ({service.framework}/{service.language})...")

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
                    f"AutoArchitect: service '{service.name}' — {status} "
                    f"({result.issues_passed}/{result.issues_total} issues)"
                )
            except AIInsufficientFunds:
                log("AutoArchitect: API account has insufficient funds — stopping.")
                raise
            except Exception as e:
                log(f"AutoArchitect: service '{service.name}' failed — {type(e).__name__}: {e}")
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
                log(f"AutoArchitect: submitting service '{service.name}' to thread pool...")
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
                        f"AutoArchitect: service '{service.name}' — {status} "
                        f"({result.issues_passed}/{result.issues_total} issues)"
                    )
                except AIInsufficientFunds:
                    log("AutoArchitect: API account has insufficient funds — stopping.")
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as e:
                    log(f"AutoArchitect: service '{service.name}' failed — {type(e).__name__}: {e}")
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
        """Dispatch an AutoEngineer for a single service."""
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

    @staticmethod
    def _build_service_prompt(
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
            f"Build ONLY this service. Use {service.language} with {service.framework}. "
            f"Focus on clean, working code with tests. "
            f"The service will run in a Docker container."
        )

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

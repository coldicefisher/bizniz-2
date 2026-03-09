"""
AutoArchitect

Takes a problem statement and project name, decomposes the system into
containerized services, creates workspaces and Dockerfiles for each,
generates a docker-compose.yml, and dispatches AutoEngineer instances
for application services (backend, frontend).
"""

import json
from pathlib import Path
from typing import Optional, Callable, List

from bizniz.base_ai_agent import BaseAIAgent
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.clients.errors import AIInsufficientFunds
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.workspace.local_workspace import LocalWorkspace
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


class AutoArchitect(BaseAIAgent):
    """
    System architect agent.

    decompose(problem_statement, project_name) → SystemArchitecture
        AI decomposes the problem into services/containers.

    build(problem_statement, project_name) → ArchitectResult
        Full pipeline: decompose → create workspaces → generate Docker
        configs → dispatch engineers for each application service.

    Parameters
    ----------
    engineer_factory:
        Callable(workspace, suggested_model) → AutoEngineer context manager.
        Used to create an engineer for each application service.
    workspace_parent:
        Parent directory where service workspaces are created.
    """

    def __init__(
        self,
        client: BaseAIClient,
        environment: BaseExecutionEnvironment,
        workspace: BaseWorkspace,
        engineer_factory: Callable,
        workspace_parent: Optional[str] = None,
        max_retries: Optional[int] = 3,
        on_event: Optional[Callable] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
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
        self._workspace_parent = workspace_parent

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
            docker_compose=raw["docker_compose"],
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
    ) -> ArchitectResult:
        """
        Full pipeline:
        1. Decompose problem into services
        2. Create workspaces for each application service
        3. Generate Dockerfiles and docker-compose.yml
        4. Dispatch AutoEngineer for each application service
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Step 1: Decompose
        architecture = self.decompose(problem_statement, project_name)

        # Step 2: Determine workspace parent
        parent = Path(self._workspace_parent) if self._workspace_parent else self._workspace.root.parent

        # Step 3: Create workspaces and Dockerfiles
        log("AutoArchitect: creating service workspaces and Dockerfiles...")
        service_workspaces = {}
        for service in architecture.services:
            if service.service_type in ("database", "cache", "proxy"):
                # Infrastructure services use standard images, no workspace needed
                continue

            ws_path = parent / service.workspace_name
            workspace = LocalWorkspace(root=str(ws_path))
            service_workspaces[service.name] = workspace

            # Generate Dockerfile
            dockerfile_content = self._generate_dockerfile(service)
            workspace.write_file("Dockerfile", dockerfile_content)

            log(f"AutoArchitect: created workspace '{service.workspace_name}' with Dockerfile")

        # Step 4: Write docker-compose.yml to the project root
        project_root = parent / architecture.project_slug
        project_root.mkdir(parents=True, exist_ok=True)
        compose_path = project_root / "docker-compose.yml"
        compose_path.write_text(architecture.docker_compose)
        log(f"AutoArchitect: wrote docker-compose.yml to {compose_path}")

        # Step 5: Dispatch engineers for application services
        log("AutoArchitect: dispatching engineers for application services...")
        service_results = []

        for service in architecture.services:
            if service.name not in service_workspaces:
                continue

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

        return ArchitectResult(
            project_name=project_name,
            architecture=architecture,
            service_results=service_results,
            docker_compose_path=str(compose_path),
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _call_ai_for_decomposition(self, user_prompt: str) -> dict:
        """Call AI for system decomposition and return parsed JSON."""
        attempts = self.max_retries
        last_error = None

        self.clear_message_history()
        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=AutoArchitectSchema,
                )
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
                continue

        raise AutoArchitectBadAIResponseError(
            f"AI failed to produce system architecture after {attempts} attempts. "
            f"Last error: {last_error}"
        )

    def _dispatch_engineer(
        self,
        workspace: LocalWorkspace,
        service: ServiceDefinition,
        service_prompt: str,
    ) -> ServiceResult:
        """Dispatch an AutoEngineer for a single service."""
        with self._engineer_factory(workspace, on_status_message=self._on_status_message) as engineer:
            analysis = engineer.analyze(service_prompt)

            results = []
            for issue in analysis.issues:
                try:
                    result = engineer.dispatch(issue.db_id)
                    results.append(result)
                except AIInsufficientFunds:
                    raise
                except Exception as e:
                    results.append(type('R', (), {
                        'success': False, 'iterations': 0,
                    })())

        successes = sum(1 for r in results if r.success)
        return ServiceResult(
            service_name=service.name,
            workspace_name=service.workspace_name,
            success=successes == len(results) and len(results) > 0,
            issues_total=len(results),
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

    @staticmethod
    def _generate_dockerfile(service: ServiceDefinition) -> str:
        """Generate a Dockerfile for a service based on its type and framework."""
        if service.language == "python":
            return (
                "FROM python:3.12-slim\n"
                "WORKDIR /app\n"
                "COPY requirements.txt .\n"
                "RUN pip install --no-cache-dir -r requirements.txt\n"
                "COPY . .\n"
                f'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{service.port or 8000}"]\n'
            )
        elif service.language == "typescript":
            if service.service_type == "frontend":
                return (
                    "FROM node:20 AS build\n"
                    "WORKDIR /app\n"
                    "COPY package*.json .\n"
                    "RUN npm ci\n"
                    "COPY . .\n"
                    "RUN npm run build\n"
                    "\n"
                    "FROM nginx:alpine\n"
                    "COPY --from=build /app/dist/ /usr/share/nginx/html/\n"
                    "EXPOSE 80\n"
                    'CMD ["nginx", "-g", "daemon off;"]\n'
                )
            else:
                return (
                    "FROM node:20-slim\n"
                    "WORKDIR /app\n"
                    "COPY package*.json .\n"
                    "RUN npm ci\n"
                    "COPY . .\n"
                    "RUN npm run build\n"
                    f'CMD ["node", "dist/main.js"]\n'
                )
        else:
            return (
                f"# Dockerfile for {service.name} ({service.framework})\n"
                f"# TODO: Configure for {service.language}\n"
            )

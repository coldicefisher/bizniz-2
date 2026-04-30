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
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Callable, List

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
from bizniz.architect.skeletons import get_skeleton, seed_workspace


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
        parallel: bool = True, max_workers: int = 4,
        layered: bool = True,
    ) -> ArchitectResult:
        """
        Full pipeline:
        1. Decompose problem into services
        2. Create project structure (infra/development/...)
        3. Generate Dockerfiles, requirements.txt, docker-compose.yml, .env
        4. Build Docker images for application services
        5. Dispatch AutoEngineer for each application service
        """
        from bizniz.project.project import Project

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Step 1: Decompose
        architecture = self.decompose(problem_statement, project_name)

        # Step 1b: Allocate free host ports (avoid clashing with anything else
        # already running on the dev machine). Modifies architecture.docker_compose
        # in place to rewrite host:container port mappings.
        port_remap = _allocate_free_ports(architecture)
        if port_remap:
            log(
                f"AutoArchitect: remapped {len(port_remap)} colliding host port(s): "
                + ", ".join(
                    f"{svc} {old}->{new}" for svc, (old, new) in port_remap.items()
                )
            )

        # Step 2: Create project structure
        parent = Path(self._project_parent) if self._project_parent else self._workspace.root.parent
        project = Project(root=parent / architecture.project_slug, project_name=project_name)
        project.create_structure()
        log(f"AutoArchitect: created project at {project.root}")

        # Step 2b: Clean up any leftover containers/images from prior builds
        # of this same project so we don't ship stale code.
        _cleanup_existing_project(architecture.project_slug, log)

        # Save architecture snapshot
        project.db.save_architecture_snapshot(
            architecture.json(),
            description=f"Initial decomposition: {len(architecture.services)} services",
        )

        # Save human-readable architecture docs
        _save_architecture_docs(project.root, architecture)

        # Step 3: Create service workspaces and Docker configs
        # Source code goes in project_root/<service>/
        # Docker configs go in infra/development/<service>/
        log("AutoArchitect: creating service workspaces and Docker configs...")
        service_workspaces = {}
        for service in architecture.services:
            if service.service_type in _INFRASTRUCTURE_TYPES:
                continue

            # Source code workspace at project root
            workspace = project.get_service_workspace(service.workspace_name)
            service_workspaces[service.name] = workspace

            # Docker config directory
            docker_dir = project.get_docker_service_dir(service.workspace_name)

            skeleton = get_skeleton(service.skeleton)
            if skeleton is not None:
                # Seed from skeleton: copy the repo, then mirror its Dockerfile
                # into infra/development/<svc>/ so docker-compose finds it where
                # the AI's compose expects.
                try:
                    copied = seed_workspace(
                        skeleton_name=skeleton.name,
                        dest=Path(workspace.root),
                        project_slug=architecture.project_slug,
                        service_name=service.name,
                    )
                    log(
                        f"AutoArchitect: seeded '{service.name}' from skeleton "
                        f"'{skeleton.name}' ({len(copied)} files)"
                    )
                    skeleton_dockerfile = Path(workspace.root) / "Dockerfile"
                    if skeleton_dockerfile.exists():
                        (docker_dir / "Dockerfile").write_text(skeleton_dockerfile.read_text())
                except FileNotFoundError as e:
                    log(f"AutoArchitect: skeleton seeding failed for '{service.name}': {e}")
                    log(f"AutoArchitect: falling back to generated boilerplate for '{service.name}'")
                    skeleton = None  # fall through to generated path below

            if skeleton is None:
                # No skeleton: generate boilerplate Dockerfile + requirements/package.json
                dockerfile_content = self._generate_dockerfile(service)
                (docker_dir / "Dockerfile").write_text(dockerfile_content)

                if service.language == "python":
                    req_content = self._generate_requirements_txt(service)
                    workspace.write_file("requirements.txt", req_content)
                elif service.language == "typescript":
                    pkg_json = self._generate_package_json(service, architecture.project_slug)
                    workspace.write_file("package.json", pkg_json)

            # Register service in project DB
            project.db.save_service(
                name=service.name,
                service_type=service.service_type,
                framework=service.framework,
                language=service.language,
                workspace_path=str(workspace.root),
            )

            log(f"AutoArchitect: created workspace '{service.workspace_name}' and Docker config")

        # Step 4: Write docker-compose.yml and .env
        project.write_docker_compose(architecture.docker_compose)
        project.write_env_file(self._generate_env_file(architecture))
        log(f"AutoArchitect: wrote docker-compose.yml and .env")

        # Step 5: Build Docker images for application services
        log("AutoArchitect: building Docker images...")
        for service in architecture.services:
            if service.name not in service_workspaces:
                continue

            workspace = service_workspaces[service.name]
            image_tag = f"{architecture.project_slug}-{service.name}:dev"

            docker_dir = project.get_docker_service_dir(service.workspace_name)
            try:
                self._build_docker_image(service, workspace, image_tag, docker_dir=docker_dir)
                service.image_name = image_tag
                project.db.update_service_image(service.name, image_tag)
                project.db.update_service_status(service.name, "ready")
                project.db.log_build_event(service.name, "image_build", True, f"Built {image_tag}")
                log(f"AutoArchitect: built image '{image_tag}'")
            except Exception as e:
                project.db.update_service_status(service.name, "failed")
                project.db.log_build_event(service.name, "image_build", False, str(e))
                log(f"AutoArchitect: image build failed for '{service.name}': {e}")

        # Step 6: Dispatch engineers for application services (in dependency order)
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
        return ArchitectResult(
            project_name=project_name,
            architecture=architecture,
            service_results=service_results,
            docker_compose_path=compose_path,
            project_root=str(project.root),
        )

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

                results = engineer.run_layered(service_prompt, analysis=analysis)
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

    def _build_docker_image(
        self, service: ServiceDefinition, workspace, image_tag: str, docker_dir=None,
    ):
        """Build the Docker image for a service.

        The Dockerfile lives in docker_dir (infra/development/<service>/)
        and the build context is the workspace root (project_root/<service>/).
        """
        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Dockerfile is in the docker config dir, build context is the workspace
        if docker_dir is not None:
            dockerfile_path = docker_dir / "Dockerfile"
        else:
            dockerfile_path = workspace.path("Dockerfile")

        if not dockerfile_path.exists():
            raise FileNotFoundError(f"Dockerfile not found at {dockerfile_path}")

        log(f"AutoArchitect: docker build {image_tag} (from {dockerfile_path})...")
        t0 = time.time()
        proc = subprocess.run(
            ["docker", "build", "-t", image_tag, "-f", str(dockerfile_path), str(workspace.root)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        elapsed = time.time() - t0
        if proc.returncode != 0:
            log(f"AutoArchitect: docker build FAILED in {elapsed:.1f}s")
            stderr_preview = proc.stderr[:300] if proc.stderr else "(no stderr)"
            log(f"AutoArchitect: build error: {stderr_preview}")
            raise RuntimeError(f"Docker build failed: {proc.stderr[:500]}")
        log(f"AutoArchitect: docker build OK in {elapsed:.1f}s")

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
                "ENV PYTHONPATH=/app\n"
                f'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{service.port or 8000}"]\n'
            )
        elif service.language == "typescript":
            # Dev image: Node + test deps, workspace mounted at runtime.
            # No COPY of source — files are bind-mounted by DockerJestEnvironment.
            return (
                "FROM node:20-slim\n"
                "WORKDIR /workspace\n"
                "COPY package*.json ./\n"
                "RUN npm install\n"
                'CMD ["npx", "jest"]\n'
            )
        else:
            return (
                f"# Dockerfile for {service.name} ({service.framework})\n"
                f"# TODO: Configure for {service.language}\n"
            )

    @staticmethod
    def _generate_requirements_txt(service: ServiceDefinition) -> str:
        """Generate requirements.txt for a Python service."""
        # Start with service-specified requirements
        packages = list(service.requirements) if service.requirements else []

        # Ensure pytest is always included for testing
        base_test_packages = ["pytest"]
        for pkg in base_test_packages:
            if pkg not in packages:
                packages.append(pkg)

        # Add framework defaults if not already specified
        framework_defaults = {
            "fastapi": ["fastapi", "uvicorn", "pydantic", "httpx"],
            "flask": ["flask"],
            "django": ["django"],
        }
        for pkg in framework_defaults.get(service.framework, []):
            if pkg not in packages:
                packages.insert(0, pkg)

        return "\n".join(packages) + "\n"

    @staticmethod
    def _generate_package_json(service: ServiceDefinition, project_slug: str) -> str:
        """Generate a minimal package.json for a TypeScript service."""
        jest_config = {
            "preset": "ts-jest",
            "roots": ["<rootDir>/src", "<rootDir>/tests"],
            "testMatch": ["**/*.test.ts", "**/*.test.tsx"],
        }
        if service.service_type == "frontend":
            jest_config["testEnvironment"] = "jest-environment-jsdom"

        pkg = {
            "name": f"{project_slug}-{service.name}",
            "version": "0.1.0",
            "private": True,
            "scripts": {
                "build": "tsc" if service.service_type != "frontend" else "vite build",
                "dev": "vite" if service.service_type == "frontend" else "ts-node src/main.ts",
                "test": "jest",
            },
            "devDependencies": {
                "jest": "^29.7.0",
                "ts-jest": "^29.1.0",
                "typescript": "^5.3.0",
                "@types/jest": "^29.5.0",
            },
            "jest": jest_config,
        }
        if service.service_type == "frontend":
            pkg["devDependencies"].update({
                "@testing-library/jest-dom": "^6.1.0",
                "@testing-library/react": "^14.1.0",
                "react": "^18.2.0",
                "react-dom": "^18.2.0",
                "@types/react": "^18.2.0",
                "@types/react-dom": "^18.2.0",
                "jest-environment-jsdom": "^29.7.0",
            })
        return json.dumps(pkg, indent=2) + "\n"

    @staticmethod
    def _generate_env_file(architecture: SystemArchitecture) -> str:
        """Generate a .env file with service connection defaults."""
        lines = [
            f"# {architecture.project_name} — development environment",
            f"PROJECT_NAME={architecture.project_slug}",
            "",
        ]

        for service in architecture.services:
            if service.service_type == "database" and service.framework == "postgres":
                lines.extend([
                    "# PostgreSQL",
                    "POSTGRES_USER=dev",
                    "POSTGRES_PASSWORD=dev",
                    f"POSTGRES_DB={architecture.project_slug}",
                    f"DATABASE_URL=postgresql://dev:dev@db:5432/{architecture.project_slug}",
                    "",
                ])
            elif service.service_type == "cache" and service.framework == "redis":
                lines.extend([
                    "# Redis",
                    "REDIS_URL=redis://redis:6379/0",
                    "",
                ])

        return "\n".join(lines) + "\n"


def _is_host_port_free(port: int) -> bool:
    """True if we can bind <port> on the host right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _find_free_port(preferred: int, taken: set) -> int:
    """Return a free host port at or above ``preferred``, skipping ``taken``."""
    port = max(preferred, 1024)
    while port < 65535:
        if port in taken or not _is_host_port_free(port):
            port += 1
            continue
        return port
    raise RuntimeError(f"No free port found at or above {preferred}")


def _allocate_free_ports(architecture: SystemArchitecture) -> Dict[str, tuple]:
    """
    Walk all services with a host port set and reassign any that collide
    with each other or with ports already bound on the host. Patches the
    ``host:container`` mappings in ``architecture.docker_compose`` for the
    affected services.

    Returns a dict of service_name -> (original_host_port, new_host_port)
    for services whose host port moved.
    """
    import yaml

    remap: Dict[str, tuple] = {}
    taken: set = set()
    name_to_remap: Dict[str, tuple] = {}
    for svc in architecture.services:
        if svc.port is None:
            continue
        free = _find_free_port(svc.port, taken)
        if free != svc.port:
            name_to_remap[svc.name] = (svc.port, free)
            remap[svc.name] = (svc.port, free)
            svc.port = free
        taken.add(free)

    if not name_to_remap:
        return remap

    # Parse compose, rewrite port mappings per service, reserialize.
    try:
        compose_data = yaml.safe_load(architecture.docker_compose) or {}
    except yaml.YAMLError:
        # Compose isn't valid YAML — leave it alone, return remap so caller
        # can log the mismatch. Service.port objects are still updated.
        return remap

    services = compose_data.get("services") or {}
    for svc_name, (old_port, new_port) in name_to_remap.items():
        svc_def = services.get(svc_name)
        if not isinstance(svc_def, dict):
            continue
        ports = svc_def.get("ports") or []
        new_ports = []
        for entry in ports:
            entry_str = str(entry)
            host, _, rest = entry_str.partition(":")
            try:
                host_int = int(host)
            except ValueError:
                new_ports.append(entry)
                continue
            if host_int == old_port:
                new_ports.append(f"{new_port}:{rest}" if rest else str(new_port))
            else:
                new_ports.append(entry)
        svc_def["ports"] = new_ports

    architecture.docker_compose = yaml.safe_dump(compose_data, sort_keys=False)
    return remap


def _cleanup_existing_project(project_slug: str, log: Callable[[str], None]) -> None:
    """
    Remove any leftover Docker images + containers from a prior build of this
    project so a fresh run isn't confused by stale state. Also cleans up orphan
    bizniz-pytest-* containers from aborted prior runs.
    """
    # Find images for this project (any tag): <slug>-<svc>:tag
    try:
        proc = subprocess.run(
            [
                "docker", "images",
                "--format", "{{.Repository}}:{{.Tag}}",
                "--filter", f"reference={project_slug}-*",
            ],
            capture_output=True, text=True, timeout=10,
        )
        images = [line for line in proc.stdout.strip().split("\n") if line]
    except Exception as e:
        log(f"AutoArchitect: cleanup image scan failed: {e}")
        images = []

    for image in images:
        try:
            cproc = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"ancestor={image}"],
                capture_output=True, text=True, timeout=10,
            )
            container_ids = [c for c in cproc.stdout.strip().split("\n") if c]
            if container_ids:
                subprocess.run(
                    ["docker", "rm", "-f"] + container_ids,
                    capture_output=True, timeout=30,
                )
                log(f"AutoArchitect: removed {len(container_ids)} container(s) using {image}")
        except Exception as e:
            log(f"AutoArchitect: container cleanup for {image} failed: {e}")

    if images:
        try:
            subprocess.run(
                ["docker", "rmi", "-f"] + images,
                capture_output=True, timeout=60,
            )
            log(f"AutoArchitect: removed {len(images)} stale image(s) for '{project_slug}'")
        except Exception as e:
            log(f"AutoArchitect: image rm failed: {e}")

    # Sweep orphan pytest containers (any project)
    try:
        proc = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=bizniz-pytest-"],
            capture_output=True, text=True, timeout=10,
        )
        ids = [c for c in proc.stdout.strip().split("\n") if c]
        if ids:
            subprocess.run(
                ["docker", "rm", "-f"] + ids,
                capture_output=True, timeout=30,
            )
            log(f"AutoArchitect: removed {len(ids)} orphan pytest container(s)")
    except Exception:
        pass


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

    lines.append("## Docker Compose")
    lines.append("")
    lines.append("```yaml")
    lines.append(architecture.docker_compose)
    lines.append("```")
    lines.append("")

    (docs_dir / "architecture.md").write_text("\n".join(lines), encoding="utf-8")

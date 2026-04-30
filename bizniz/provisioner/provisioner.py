"""
Provisioner — turns a SystemArchitecture into a real project on disk.

The architect plans (services, ports, frameworks, depends_on). The
Provisioner takes that plan and produces:

  1. ``project_root/`` directory tree
  2. Source code workspaces, seeded from skeletons when applicable
  3. ``infra/development/`` containing per-service Dockerfile dirs,
     templated infrastructure config (postgres init.sql, FusionAuth
     kickstart YAML, etc.), the docker-compose.yml, and the .env file
  4. Built Docker images for application services

Pure planning concerns stay in the architect. Pure materialization
concerns live here.
"""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.architect.types import (
    ServiceDefinition,
    SystemArchitecture,
)
from bizniz.architect.skeletons import get_skeleton, seed_workspace
from bizniz.project.project import Project
from bizniz.provisioner.compose_builder import build_compose
from bizniz.provisioner.docker_builder import build_image
from bizniz.provisioner.env_builder import build_env_file
from bizniz.provisioner.templates import lookup as lookup_template
from bizniz.provisioner.templates.base import TemplateContext, TemplateOutput
from bizniz.provisioner.types import (
    ProvisionedService,
    ProvisionResult,
    ProvisionerError,
)


_INFRASTRUCTURE_TYPES = {"database", "cache", "proxy", "auth"}
_APP_TYPES = {"backend", "frontend", "worker"}


class Provisioner:
    """Materializes a SystemArchitecture as a real project on disk + images.

    Parameters
    ----------
    project_parent:
        Parent directory under which ``project_root`` is created.
    on_status_message:
        Optional log callback.
    build_images:
        When False, skip the docker-build phase (useful in tests).
    """

    def __init__(
        self,
        project_parent: str | Path,
        on_status_message: Optional[Callable[[str], None]] = None,
        build_images: bool = True,
    ):
        self._project_parent = Path(project_parent)
        self._on_status_message = on_status_message
        self._build_images = build_images

    # ── Public API ────────────────────────────────────────────────────────────

    def provision(
        self,
        architecture: SystemArchitecture,
        project_name: str,
    ) -> ProvisionResult:
        """Run the full provisioning sequence and return a ProvisionResult."""
        log = self._log

        # 1. Free-port allocation across all host-port-bearing services.
        port_remap = self._allocate_free_ports(architecture)
        if port_remap:
            log(
                f"Provisioner: remapped {len(port_remap)} colliding host port(s): "
                + ", ".join(
                    f"{svc} {old}->{new}" for svc, (old, new) in port_remap.items()
                )
            )

        # 2. Project root + dependency cleanup.
        project = Project(
            root=self._project_parent / architecture.project_slug,
            project_name=project_name,
        )
        project.create_structure()
        log(f"Provisioner: created project at {project.root}")

        self._cleanup_existing_project(architecture.project_slug)

        # Snapshot architecture to project DB.
        project.db.save_architecture_snapshot(
            architecture.json(),
            description=f"Initial decomposition: {len(architecture.services)} services",
        )

        # 3. Per-service materialization. Walk in two passes — render all
        # template outputs first, then write to disk in one batch so
        # dependency-aware merges (e.g. postgres injecting fusionauth DB)
        # are visible to compose generation.
        template_outputs: Dict[str, TemplateOutput] = {}
        provisioned_services: List[ProvisionedService] = []

        for service in architecture.services:
            ps = self._provision_service(service, architecture, project, template_outputs)
            provisioned_services.append(ps)

        # 4. Compose + .env
        compose_yaml = build_compose(architecture, template_outputs, architecture.project_slug)
        env_text = build_env_file(
            architecture,
            self._collect_env_vars(template_outputs),
        )
        compose_path = project.dev_root / "docker-compose.yml"
        env_path = project.dev_root / ".env"
        project.dev_root.mkdir(parents=True, exist_ok=True)
        compose_path.write_text(compose_yaml)
        env_path.write_text(env_text)
        log("Provisioner: wrote docker-compose.yml and .env")

        # 5. Build images for app services
        if self._build_images:
            for ps in provisioned_services:
                if ps.is_infrastructure or ps.workspace_path is None:
                    continue
                image_tag = f"{architecture.project_slug}-{ps.name}:dev"
                docker_dir = project.get_docker_service_dir(ps.workspace_name)
                dockerfile = docker_dir / "Dockerfile"
                try:
                    build_image(
                        image_tag=image_tag,
                        context=Path(ps.workspace_path),
                        dockerfile=dockerfile,
                        log=self._on_status_message,
                    )
                    ps.image_name = image_tag
                    ps.image_built = True
                    project.db.update_service_image(ps.name, image_tag)
                    project.db.update_service_status(ps.name, "ready")
                    project.db.log_build_event(ps.name, "image_build", True, f"Built {image_tag}")
                except Exception as e:
                    project.db.update_service_status(ps.name, "failed")
                    project.db.log_build_event(ps.name, "image_build", False, str(e))
                    log(f"Provisioner: image build failed for '{ps.name}': {e}")

        return ProvisionResult(
            project_name=project_name,
            project_slug=architecture.project_slug,
            project_root=str(project.root),
            compose_path=str(compose_path),
            env_path=str(env_path),
            services=provisioned_services,
            port_remap=port_remap,
        )

    # ── Per-service materialization ───────────────────────────────────────────

    def _provision_service(
        self,
        service: ServiceDefinition,
        architecture: SystemArchitecture,
        project: Project,
        template_outputs: Dict[str, TemplateOutput],
    ) -> ProvisionedService:
        log = self._log

        # Infrastructure: render template, write infra files, register
        # service in project DB, no workspace.
        if service.service_type in _INFRASTRUCTURE_TYPES:
            template = lookup_template(service.framework)
            if template is None:
                log(
                    f"Provisioner: no template for infrastructure service "
                    f"'{service.name}' (framework={service.framework}); compose entry "
                    f"will be missing for it."
                )
                return ProvisionedService(
                    name=service.name,
                    workspace_name=service.workspace_name,
                    is_infrastructure=True,
                    template_name=None,
                )

            ctx = TemplateContext(
                service=service,
                project_slug=architecture.project_slug,
                project_root=project.root,
            )
            output = template.render(ctx)
            template_outputs[service.name] = output

            # Write infra files under project_root/infra/development/
            self._write_infra_files(project.dev_root, output.infra_files)

            project.db.save_service(
                name=service.name,
                service_type=service.service_type,
                framework=service.framework,
                language=service.language,
                workspace_path="",  # no app workspace for infra
            )
            log(f"Provisioner: rendered '{service.name}' from {template.name} template")
            return ProvisionedService(
                name=service.name,
                workspace_name=service.workspace_name,
                is_infrastructure=True,
                template_name=template.name,
            )

        # Application service. Two paths: skeleton-seeded or generated.
        workspace = project.get_service_workspace(service.workspace_name)
        docker_dir = project.get_docker_service_dir(service.workspace_name)
        skeleton = get_skeleton(service.skeleton)
        skeleton_used: Optional[str] = None

        if skeleton is not None:
            try:
                copied = seed_workspace(
                    skeleton_name=skeleton.name,
                    dest=Path(workspace.root),
                    project_slug=architecture.project_slug,
                    service_name=service.name,
                )
                log(
                    f"Provisioner: seeded '{service.name}' from skeleton "
                    f"'{skeleton.name}' ({len(copied)} files)"
                )
                # Mirror skeleton's Dockerfile into the infra dir so compose
                # finds it where it expects.
                skeleton_dockerfile = Path(workspace.root) / "Dockerfile"
                if skeleton_dockerfile.exists():
                    (docker_dir / "Dockerfile").write_text(skeleton_dockerfile.read_text())
                skeleton_used = skeleton.name
            except FileNotFoundError as e:
                log(
                    f"Provisioner: skeleton seeding failed for '{service.name}' ({e}); "
                    f"falling back to generated boilerplate"
                )
                skeleton = None

        if skeleton is None:
            # Generated boilerplate via app-* template.
            sentinel = "__python_app__" if service.language == "python" else "__typescript_app__"
            template = lookup_template(sentinel)
            if template is None:
                raise ProvisionerError(
                    f"No app template registered for language '{service.language}'"
                )
            ctx = TemplateContext(
                service=service,
                project_slug=architecture.project_slug,
                project_root=project.root,
            )
            output = template.render(ctx)
            for rel, content in output.workspace_files.items():
                workspace.write_file(rel, content)
            for rel, content in output.infra_files.items():
                target = docker_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            log(f"Provisioner: generated boilerplate for '{service.name}' ({sentinel})")

        project.db.save_service(
            name=service.name,
            service_type=service.service_type,
            framework=service.framework,
            language=service.language,
            workspace_path=str(workspace.root),
        )
        return ProvisionedService(
            name=service.name,
            workspace_name=service.workspace_name,
            workspace_path=str(workspace.root),
            is_infrastructure=False,
            skeleton_name=skeleton_used,
        )

    def _write_infra_files(self, dev_root: Path, files: Dict[str, str]) -> None:
        for rel, content in files.items():
            target = dev_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    @staticmethod
    def _collect_env_vars(template_outputs: Dict[str, TemplateOutput]) -> Dict[str, str]:
        env: Dict[str, str] = {}
        for out in template_outputs.values():
            env.update(out.env_vars)
        return env

    # ── Free-port allocation (was in architect) ───────────────────────────────

    def _allocate_free_ports(self, architecture: SystemArchitecture) -> Dict[str, tuple]:
        """Reassign any host ports that collide with each other or with
        something already bound on the dev machine. Mutates each service's
        ``port`` field in place."""
        remap: Dict[str, tuple] = {}
        taken: set = set()
        for svc in architecture.services:
            if svc.port is None:
                continue
            free = _find_free_port(svc.port, taken)
            if free != svc.port:
                remap[svc.name] = (svc.port, free)
                svc.port = free
            taken.add(free)
        return remap

    # ── Project cleanup (was in architect) ────────────────────────────────────

    def _cleanup_existing_project(self, project_slug: str) -> None:
        """Remove leftover Docker images + containers from prior builds of
        this project so a fresh run isn't confused by stale state. Best
        effort — failures are logged, never raised."""
        log = self._log
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
            log(f"Provisioner: image scan failed: {e}")
            images = []

        for image in images:
            try:
                cproc = subprocess.run(
                    ["docker", "ps", "-aq", "--filter", f"ancestor={image}"],
                    capture_output=True, text=True, timeout=10,
                )
                ids = [c for c in cproc.stdout.strip().split("\n") if c]
                if ids:
                    subprocess.run(
                        ["docker", "rm", "-f"] + ids,
                        capture_output=True, timeout=30,
                    )
                    log(f"Provisioner: removed {len(ids)} container(s) using {image}")
            except Exception as e:
                log(f"Provisioner: container cleanup for {image} failed: {e}")

        if images:
            try:
                subprocess.run(
                    ["docker", "rmi", "-f"] + images,
                    capture_output=True, timeout=60,
                )
                log(f"Provisioner: removed {len(images)} stale image(s) for '{project_slug}'")
            except Exception as e:
                log(f"Provisioner: image rm failed: {e}")

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
                log(f"Provisioner: removed {len(ids)} orphan pytest container(s)")
        except Exception:
            pass

    # ── Logging helper ────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._on_status_message:
            self._on_status_message(msg)


# ── Module-level helpers used by free-port allocation ────────────────────────

def _is_host_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _find_free_port(preferred: int, taken: set) -> int:
    port = max(preferred, 1024)
    while port < 65535:
        if port in taken or not _is_host_port_free(port):
            port += 1
            continue
        return port
    raise RuntimeError(f"No free port found at or above {preferred}")

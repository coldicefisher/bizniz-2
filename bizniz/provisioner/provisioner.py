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
from bizniz.provisioner.ai_fallback import (
    AIClientFactory,
    AIFallbackTemplate,
    generate_ai_fallback_response,
)
from bizniz.provisioner.ai_recovery import try_ai_recovery
from bizniz.provisioner.compose_builder import build_compose
from bizniz.provisioner.docker_builder import build_image
from bizniz.provisioner.env_builder import build_env_file
from bizniz.provisioner.templates import lookup as lookup_template
from bizniz.provisioner.templates.base import (
    InfraTemplate,
    TemplateContext,
    TemplateOutput,
)
from bizniz.provisioner.types import (
    ProbedService,
    ProvisionedService,
    ProvisionResult,
    ProvisionState,
    ProvisionerError,
    ReconcileAction,
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
    ai_client_factory:
        Optional ``Callable[[model_name: str], BaseAIClient]``. Required
        when either AI escape hatch is enabled. Lets the caller decide
        the model per call without coupling the Provisioner to
        ``BiznizConfig``.
    ai_fallback_enabled:
        When True, infrastructure services with no registered template
        get an AI-generated fallback (model: ``ai_fallback_model``).
        Cached at ``~/.bizniz/template_cache``. Defaults to False.
    ai_fallback_model:
        Model name passed to ``ai_client_factory`` for the fallback call.
        Defaults to ``"gemini-flash"``.
    ai_recovery_enabled:
        When True, failed Docker builds are retried with an AI-patched
        Dockerfile (model: ``ai_recovery_model``). Defaults to False.
    ai_recovery_model:
        Model name for recovery calls. Defaults to ``"gemini-pro"``.
    ai_recovery_max_retries:
        Hard cap on AI recovery retries per service. Defaults to 2.
    ai_template_cache_dir:
        Override the template cache location (otherwise uses
        ``BIZNIZ_TEMPLATE_CACHE_DIR`` env var or
        ``~/.bizniz/template_cache``).
    """

    def __init__(
        self,
        project_parent: str | Path,
        on_status_message: Optional[Callable[[str], None]] = None,
        build_images: bool = True,
        ai_client_factory: Optional["AIClientFactory"] = None,
        ai_fallback_enabled: bool = False,
        ai_fallback_model: str = "gemini-flash",
        ai_recovery_enabled: bool = False,
        ai_recovery_model: str = "gemini-pro",
        ai_recovery_max_retries: int = 2,
        ai_template_cache_dir: Optional[Path] = None,
    ):
        self._project_parent = Path(project_parent)
        self._on_status_message = on_status_message
        self._build_images = build_images

        if (ai_fallback_enabled or ai_recovery_enabled) and ai_client_factory is None:
            raise ProvisionerError(
                "ai_fallback_enabled / ai_recovery_enabled require "
                "ai_client_factory to be set."
            )
        self._ai_client_factory = ai_client_factory
        self._ai_fallback_enabled = ai_fallback_enabled
        self._ai_fallback_model = ai_fallback_model
        self._ai_recovery_enabled = ai_recovery_enabled
        self._ai_recovery_model = ai_recovery_model
        self._ai_recovery_max_retries = ai_recovery_max_retries
        self._ai_template_cache_dir = ai_template_cache_dir

    # ── Public API ────────────────────────────────────────────────────────────

    def provision(
        self,
        architecture: SystemArchitecture,
        project_name: str,
        prune: bool = False,
    ) -> ProvisionResult:
        """Probe → reconcile → materialize.

        Always idempotent: probes the project root, reconciles the desired
        architecture against observed state, and materializes whatever is
        new or extended. Existing services are preserved.

        Pass ``prune=True`` to also delete orphan Docker images (services
        that exist in observed state but not in the desired architecture
        — e.g. removed during a refactor). Off by default to avoid
        clobbering work between runs.
        """
        log = self._log

        # 1. Project root (idempotent).
        project = Project(
            root=self._project_parent / architecture.project_slug,
            project_name=project_name,
        )
        project.create_structure()

        # 2. Probe observed state.
        state = self.probe(architecture.project_slug, project.root)
        log(
            f"Provisioner: probed '{architecture.project_slug}' — "
            f"{sum(1 for s in state.services if s.db_recorded)} known services, "
            f"{len(state.orphan_workspace_dirs)} orphan workspace(s), "
            f"{len(state.project_images)} project image(s)"
        )

        # 3. Reconcile desired vs observed → per-service action plan.
        actions = self._reconcile(architecture, state)
        action_by_name = {a.service_name: a for a in actions}

        # 4. Port allocation: only re-map ports for services that
        #    reconciler flagged "create" (truly new). Preserved services
        #    keep their existing ports.
        port_remap = self._allocate_ports_for_new(architecture, action_by_name)
        if port_remap:
            log(
                f"Provisioner: remapped {len(port_remap)} colliding host port(s): "
                + ", ".join(
                    f"{svc} {old}->{new}" for svc, (old, new) in port_remap.items()
                )
            )

        # 5. Snapshot new architecture to project DB.
        try:
            project.db.save_architecture_snapshot(
                architecture.json(),
                description=self._snapshot_description(actions),
            )
        except Exception as e:
            log(f"Provisioner: snapshot failed ({e}) — continuing")

        # 6. Per-service materialization, dispatched by reconciled action.
        template_outputs: Dict[str, TemplateOutput] = {}
        provisioned_services: List[ProvisionedService] = []

        for service in architecture.services:
            action = action_by_name[service.name]
            if action.action == "create":
                ps = self._provision_service(
                    service, architecture, project, template_outputs,
                )
            else:
                # update / preserve — re-render templates idempotently,
                # don't re-seed app workspaces.
                ps = self._evolve_service(
                    service, architecture, project, template_outputs,
                )
            provisioned_services.append(ps)

        # 7. Compose + .env (always regenerated from desired architecture).
        compose_yaml = build_compose(architecture, template_outputs, architecture.project_slug)
        env_text = build_env_file(
            architecture, self._collect_env_vars(template_outputs),
        )
        compose_path = project.dev_root / "docker-compose.yml"
        env_path = project.dev_root / ".env"
        project.dev_root.mkdir(parents=True, exist_ok=True)
        compose_path.write_text(compose_yaml)
        env_path.write_text(env_text)
        log("Provisioner: wrote docker-compose.yml and .env")

        # 8. Build images per reconciled action.
        if self._build_images:
            self._build_images_per_action(
                provisioned_services, architecture, project, action_by_name,
            )

        # 9. Optional: prune orphan images for services no longer in
        #    the desired architecture.
        if prune:
            desired_names = {s.name for s in architecture.services}
            self._prune_orphan_images(state, desired_names)

        return ProvisionResult(
            project_name=project_name,
            project_slug=architecture.project_slug,
            project_root=str(project.root),
            compose_path=str(compose_path),
            env_path=str(env_path),
            services=provisioned_services,
            port_remap=port_remap,
        )

    def evolve(
        self,
        architecture: SystemArchitecture,
        project_name: str,
    ) -> ProvisionResult:
        """Idempotent re-provision for an existing project.

        Thin alias for ``provision(prune=False)`` — preserved for
        readability at call sites that want to signal "this is an
        evolve, not a fresh build".
        """
        return self.provision(architecture, project_name, prune=False)

    def probe(
        self,
        project_slug: str,
        project_root: Optional[Path] = None,
    ) -> ProvisionState:
        """Read DB + filesystem + Docker to snapshot the current state of a
        project. Pure observation — no side effects.

        ``project_root`` defaults to ``project_parent / project_slug``.
        """
        if project_root is None:
            project_root = self._project_parent / project_slug
        project_root = Path(project_root)

        state = ProvisionState(
            project_slug=project_slug,
            project_root=str(project_root),
            project_root_exists=project_root.exists(),
        )
        if not project_root.exists():
            return state

        dev_root = project_root / "infra" / "development"
        state.compose_exists = (dev_root / "docker-compose.yml").exists()
        state.env_exists = (dev_root / ".env").exists()

        # DB state — only if .bizniz/project.db exists, otherwise probe
        # FS + Docker only.
        db_path = project_root / ".bizniz" / "project.db"
        services_by_name: Dict[str, ProbedService] = {}
        if db_path.exists():
            try:
                from bizniz.project.project_db import ProjectDB

                project = Project(root=project_root, project_name=project_slug)
                latest = project.db.get_latest_architecture()
                if latest is not None:
                    state.last_architecture_snapshot_json = latest["snapshot_json"]
                for row in project.db.get_services():
                    services_by_name[row["name"]] = ProbedService(
                        name=row["name"],
                        db_recorded=True,
                        db_workspace_path=row["workspace_path"] or None,
                        db_status=row["status"] if "status" in row.keys() else None,
                        db_image_name=row["image_name"] or None,
                    )
            except Exception as e:
                self._log(f"Provisioner: probe — DB read failed ({e})")

        # Filesystem scan: every directory under project_root that isn't
        # infra/.bizniz is a candidate workspace.
        fs_workspaces: set = set()
        if project_root.exists():
            for child in project_root.iterdir():
                if not child.is_dir():
                    continue
                if child.name in (".bizniz", "infra"):
                    continue
                fs_workspaces.add(child.name)

        # Mark workspace_exists / has_dockerfile per known service.
        for name, svc in services_by_name.items():
            ws_dir = project_root / name
            svc.workspace_exists_on_disk = ws_dir.exists() and ws_dir.is_dir()
            svc.has_dockerfile = (dev_root / name / "Dockerfile").exists()

        # Orphan workspaces: on disk, not in DB.
        state.orphan_workspace_dirs = sorted(
            fs_workspaces - set(services_by_name.keys())
        )

        # Docker image inventory.
        state.project_images = self._list_project_images(project_slug)
        for name, svc in services_by_name.items():
            expected_tag = f"{project_slug}-{name}:dev"
            svc.image_in_docker = expected_tag in state.project_images

        state.services = list(services_by_name.values())
        return state

    def _reconcile(
        self,
        architecture: SystemArchitecture,
        state: ProvisionState,
    ) -> List[ReconcileAction]:
        """Decide what to do with each service based on desired vs
        observed.

        Logic per service:
          - Not in DB AND no workspace on disk → ``create`` (true new
            service or first-time build).
          - In DB but workspace missing on disk → ``create`` (state drift
            — DB says it exists, FS disagrees, rebuild from scratch).
          - In DB and on disk, architect tagged ``new`` → defensive
            ``create`` (architect thinks new, but state shows we already
            built it; treat as create to materialize anything missing
            but don't re-seed if files exist).
          - In DB and on disk, architect tagged ``extended`` → ``update``
            (re-render templates, leave app workspace alone).
          - Otherwise → ``preserve``.

        Image rebuild is set when ``create`` or ``update``, OR when the
        DB says no image is recorded yet for an existing service.
        """
        actions: List[ReconcileAction] = []
        for svc in architecture.services:
            probed = state.get_service(svc.name)
            arch_state = svc.evolve_state or "unchanged"

            if probed is None:
                action = "create"
                reason = "not present in observed state"
            elif not probed.workspace_exists_on_disk and svc.service_type in _APP_TYPES:
                action = "create"
                reason = "DB-recorded but workspace missing on disk"
            elif arch_state == "new":
                action = "create"
                reason = "architect tagged new"
            elif arch_state == "extended":
                action = "update"
                reason = "architect tagged extended"
            else:
                action = "preserve"
                reason = "unchanged"

            rebuild = action in ("create", "update")
            if (
                action == "preserve"
                and svc.service_type in _APP_TYPES
                and probed is not None
                and not probed.image_in_docker
            ):
                rebuild = True
                reason += "; image missing — rebuild"

            actions.append(
                ReconcileAction(
                    service_name=svc.name,
                    action=action,
                    rebuild_image=rebuild,
                    reason=reason,
                )
            )
        return actions

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
            template = self._resolve_infra_template(service)
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
                    on_status=log,
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

    def _evolve_service(
        self,
        service: ServiceDefinition,
        architecture: SystemArchitecture,
        project: Project,
        template_outputs: Dict[str, TemplateOutput],
    ) -> ProvisionedService:
        """Per-service materialization for evolve mode.

        Decision tree:
          - Infrastructure service (any state) → re-render template
            (idempotent, templates are pure functions). Write infra
            files (overwrites are fine — the renderer is deterministic).
          - App service, evolve_state == "new" → same as fresh
            provision: seed skeleton or render app template, register
            in DB.
          - App service, "extended" or "unchanged" → DO NOT re-seed.
            The workspace already has user / engineer-generated code.
            Only refresh the infra Dockerfile if its content drifted
            (skeleton path) and ensure the DB row exists.
        """
        log = self._log
        state = service.evolve_state or "unchanged"

        # Infrastructure: render template every time. Idempotent because
        # template output is a pure function of the architecture; writes
        # are deterministic.
        if service.service_type in _INFRASTRUCTURE_TYPES:
            template = self._resolve_infra_template(service)
            if template is None:
                log(
                    f"Provisioner: evolve — no template for '{service.name}' "
                    f"(framework={service.framework}); skipping compose entry"
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
            self._write_infra_files(project.dev_root, output.infra_files)
            project.db.save_service(
                name=service.name,
                service_type=service.service_type,
                framework=service.framework,
                language=service.language,
                workspace_path="",
            )
            log(f"Provisioner: evolve re-rendered '{service.name}' ({template.name}, {state})")
            return ProvisionedService(
                name=service.name,
                workspace_name=service.workspace_name,
                is_infrastructure=True,
                template_name=template.name,
            )

        # App service.
        workspace = project.get_service_workspace(service.workspace_name)
        docker_dir = project.get_docker_service_dir(service.workspace_name)

        if state == "new":
            # Same path as fresh provision. Reuse the existing
            # _provision_service implementation by calling it directly.
            return self._provision_service(
                service, architecture, project, template_outputs,
            )

        # Extended or unchanged. Don't re-seed; the workspace already
        # has generated code. Make sure the Dockerfile is in place
        # (skeleton-seeded services keep theirs at the workspace root;
        # mirror it into infra/development if missing).
        if get_skeleton(service.skeleton):
            workspace_dockerfile = Path(workspace.root) / "Dockerfile"
            infra_dockerfile = docker_dir / "Dockerfile"
            if (
                workspace_dockerfile.exists()
                and (
                    not infra_dockerfile.exists()
                    or workspace_dockerfile.read_text() != infra_dockerfile.read_text()
                )
            ):
                infra_dockerfile.write_text(workspace_dockerfile.read_text())
                log(f"Provisioner: evolve refreshed Dockerfile for '{service.name}'")

        # Ensure DB row exists.
        project.db.save_service(
            name=service.name,
            service_type=service.service_type,
            framework=service.framework,
            language=service.language,
            workspace_path=str(workspace.root),
        )

        log(f"Provisioner: evolve preserved '{service.name}' ({state})")
        return ProvisionedService(
            name=service.name,
            workspace_name=service.workspace_name,
            workspace_path=str(workspace.root),
            is_infrastructure=False,
            skeleton_name=service.skeleton if service.skeleton != "none" else None,
        )

    def _allocate_ports_for_new(
        self,
        architecture: SystemArchitecture,
        action_by_name: Dict[str, ReconcileAction],
    ) -> Dict[str, tuple]:
        """Reassign host ports that collide with each other or the dev
        machine, but only for services the reconciler flagged ``create``.
        Preserved services keep their original ports."""
        remap: Dict[str, tuple] = {}
        taken: set = {
            s.port for s in architecture.services
            if s.port is not None
            and action_by_name[s.name].action != "create"
        }
        for svc in architecture.services:
            if svc.port is None:
                continue
            if action_by_name[svc.name].action != "create":
                continue
            free = _find_free_port(svc.port, taken)
            if free != svc.port:
                remap[svc.name] = (svc.port, free)
                svc.port = free
            taken.add(free)
        return remap

    @staticmethod
    def _snapshot_description(actions: List[ReconcileAction]) -> str:
        c = sum(1 for a in actions if a.action == "create")
        u = sum(1 for a in actions if a.action == "update")
        p = sum(1 for a in actions if a.action == "preserve")
        return f"Reconciled: {c} create, {u} update, {p} preserve"

    def _build_images_per_action(
        self,
        provisioned_services: List[ProvisionedService],
        architecture: SystemArchitecture,
        project: Project,
        action_by_name: Dict[str, ReconcileAction],
    ) -> None:
        log = self._log
        for ps in provisioned_services:
            if ps.is_infrastructure or ps.workspace_path is None:
                continue
            action = action_by_name[ps.name]
            if not action.rebuild_image:
                continue
            image_tag = f"{architecture.project_slug}-{ps.name}:dev"
            docker_dir = project.get_docker_service_dir(ps.workspace_name)
            dockerfile = docker_dir / "Dockerfile"

            def _do_build():
                build_image(
                    image_tag=image_tag,
                    context=Path(ps.workspace_path),
                    dockerfile=dockerfile,
                    log=self._on_status_message,
                )

            try:
                _do_build()
                self._record_image_built(project, ps, image_tag, action.action)
            except Exception as e:
                log(f"Provisioner: image build failed for '{ps.name}': {e}")
                if self._try_ai_recovery_for_build(
                    dockerfile_path=dockerfile,
                    build_error=str(e),
                    rebuild=_do_build,
                ):
                    self._record_image_built(
                        project, ps, image_tag,
                        f"{action.action}+ai_recovery",
                    )
                else:
                    project.db.update_service_status(ps.name, "failed")
                    project.db.log_build_event(ps.name, "image_build", False, str(e))

    def _record_image_built(
        self,
        project: Project,
        ps: ProvisionedService,
        image_tag: str,
        action_label: str,
    ) -> None:
        ps.image_name = image_tag
        ps.image_built = True
        project.db.update_service_image(ps.name, image_tag)
        project.db.update_service_status(ps.name, "ready")
        project.db.log_build_event(
            ps.name, "image_build", True, f"{action_label}: built {image_tag}",
        )

    def _try_ai_recovery_for_build(
        self,
        dockerfile_path: Path,
        build_error: str,
        rebuild: Callable[[], None],
    ) -> bool:
        if not self._ai_recovery_enabled or self._ai_client_factory is None:
            return False
        try:
            client = self._ai_client_factory(self._ai_recovery_model)
        except Exception as e:
            self._log(f"Provisioner: AI recovery client construction failed ({e})")
            return False
        return try_ai_recovery(
            client=client,
            dockerfile_path=dockerfile_path,
            build_error=build_error,
            rebuild=rebuild,
            max_retries=self._ai_recovery_max_retries,
            on_status=self._on_status_message,
        )

    def _resolve_infra_template(
        self, service: ServiceDefinition,
    ) -> Optional[InfraTemplate]:
        """Static registry lookup, with optional AI fallback.

        Returns ``None`` only when the static registry misses AND
        AI fallback is disabled / fails. Caller decides what to do
        with that — currently logs and emits a service with no
        compose entry.
        """
        template = lookup_template(service.framework)
        if template is not None:
            return template
        if not self._ai_fallback_enabled or self._ai_client_factory is None:
            return None
        return self._build_ai_fallback_template(service)

    def _build_ai_fallback_template(
        self, service: ServiceDefinition,
    ) -> Optional[InfraTemplate]:
        log = self._log
        try:
            client = self._ai_client_factory(self._ai_fallback_model)
        except Exception as e:
            log(f"Provisioner: AI fallback client construction failed ({e})")
            return None
        try:
            log(
                f"Provisioner: AI fallback for '{service.name}' "
                f"(framework={service.framework}, type={service.service_type}, "
                f"model={self._ai_fallback_model})"
            )
            response = generate_ai_fallback_response(
                client=client,
                framework=service.framework,
                service_type=service.service_type,
                description=service.description,
                cache_dir=self._ai_template_cache_dir,
            )
        except Exception as e:
            log(
                f"Provisioner: AI fallback for '{service.framework}' failed "
                f"({e}); falling through to no-template path"
            )
            return None
        kind = "upstream_image" if response.upstream_image else "dockerfile"
        log(
            f"Provisioner: AI fallback produced {kind} for '{service.name}' — "
            f"{response.notes[:160]}"
        )
        return AIFallbackTemplate(response, framework=service.framework)

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

    # ── Docker introspection / pruning ────────────────────────────────────────

    def _list_project_images(self, project_slug: str) -> List[str]:
        try:
            proc = subprocess.run(
                [
                    "docker", "images",
                    "--format", "{{.Repository}}:{{.Tag}}",
                    "--filter", f"reference={project_slug}-*",
                ],
                capture_output=True, text=True, timeout=10,
            )
            return [line for line in proc.stdout.strip().split("\n") if line]
        except Exception as e:
            self._log(f"Provisioner: image scan failed: {e}")
            return []

    def _prune_orphan_images(
        self, state: ProvisionState, desired_names: set,
    ) -> None:
        """Remove docker images for services that are no longer in the
        desired architecture (e.g. removed by a refactor). Best effort —
        failures are logged, never raised.
        """
        log = self._log
        slug = state.project_slug
        # An image's service name is the suffix after "<slug>-" and before ":".
        orphan_images: List[str] = []
        for image in state.project_images:
            if not image.startswith(f"{slug}-"):
                continue
            name_and_tag = image[len(slug) + 1 :]
            service_part = name_and_tag.split(":", 1)[0]
            if service_part not in desired_names:
                orphan_images.append(image)

        for image in orphan_images:
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
                    log(f"Provisioner: prune — removed {len(ids)} container(s) using {image}")
            except Exception as e:
                log(f"Provisioner: prune — container cleanup for {image} failed: {e}")

        if orphan_images:
            try:
                subprocess.run(
                    ["docker", "rmi", "-f"] + orphan_images,
                    capture_output=True, timeout=60,
                )
                log(
                    f"Provisioner: prune — removed {len(orphan_images)} orphan "
                    f"image(s) for '{slug}': {', '.join(orphan_images)}"
                )
            except Exception as e:
                log(f"Provisioner: prune — image rm failed: {e}")

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

"""Tests for the deterministic docker-compose builder."""
import yaml

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner.compose_builder import build_compose
from bizniz.provisioner.templates.base import TemplateOutput


def _arch(*services) -> SystemArchitecture:
    return SystemArchitecture(
        project_name="X",
        project_slug="x",
        services=list(services),
        description="t",
    )


def _svc(name, type_, framework, language, port=None, depends_on=None, skeleton="none") -> ServiceDefinition:
    return ServiceDefinition(
        name=name, service_type=type_, framework=framework, language=language,
        description=name, workspace_name=name, port=port,
        depends_on=depends_on or [], requirements=[], skeleton=skeleton,
    )


def test_app_service_only_produces_build_entry():
    arch = _arch(_svc("backend", "backend", "fastapi", "python", port=8001, skeleton="fastapi"))
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    backend = parsed["services"]["backend"]
    assert backend["image"] == "x-backend:dev"
    assert backend["build"]["context"] == "../../backend"
    # dockerfile is relative to the build context (compose semantics),
    # so only ONE ../ to climb out of the workspace before descending.
    assert backend["build"]["dockerfile"] == "../infra/development/backend/Dockerfile"
    assert "8001:8000" in backend["ports"]
    assert "../../backend:/app" in backend["volumes"]
    assert backend["env_file"] == ".env"
    # Python services don't get the node_modules anonymous volume.
    assert "/app/node_modules" not in backend["volumes"]
    # network registered
    assert "app-network" in parsed["networks"]


def test_compose_pins_project_name_to_slug():
    """Compose project name MUST be the slug, not the parent directory.

    Without this, every bizniz-generated project at `infra/development/`
    inherits project name `development` from the parent dir, and
    `docker compose up` for project A would stomp on project B's
    containers (and `down` would tear down the wrong stack)."""
    arch = _arch(
        _svc("postgres", "database", "postgres", "sql", port=5433),
    )
    yml = build_compose(arch, template_outputs={}, project_slug="vehinexa")
    parsed = yaml.safe_load(yml)
    assert parsed["name"] == "vehinexa"


def test_skeleton_container_port_overrides_framework_default():
    """saas-frontend (angular) declares container_port=5173 (matches its
    dev server) — compose must use that, not the angular framework default
    of 4200."""
    arch = _arch(_svc("frontend", "frontend", "angular", "typescript",
                      port=5177, skeleton="saas-frontend"))
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    frontend = parsed["services"]["frontend"]
    assert "5177:5173" in frontend["ports"], (
        f"Expected host:5177 → container:5173 from skeleton override, "
        f"got {frontend['ports']}"
    )


def test_typescript_app_service_preserves_node_modules_with_anon_volume():
    """Without an anonymous volume on /app/node_modules, the host
    workspace bind-mount masks the npm-installed deps and `npm run dev`
    fails with `vite: not found`."""
    arch = _arch(
        _svc("frontend", "frontend", "react", "typescript",
             port=5173, skeleton="react"),
    )
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    frontend = parsed["services"]["frontend"]
    assert "../../frontend:/app" in frontend["volumes"]
    assert "/app/node_modules" in frontend["volumes"]


# ── core/ shared-library mounts (Refactorer item 6 contract) ─────


def test_python_service_mounts_core_python_and_sets_pythonpath():
    """Every Python service mounts ``core/python`` at ``/python_core``
    and gets ``PYTHONPATH`` pointing to it. Refactorer extractions
    drop code into ``core/python``; consumer services pick it up via
    ``from python_core.<feature> import ...``."""
    arch = _arch(_svc("backend", "backend", "fastapi", "python",
                      port=8001, skeleton="fastapi"))
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    backend = parsed["services"]["backend"]
    assert "../../core/python:/python_core" in backend["volumes"]
    # PYTHONPATH prefers /python_core so shared imports win over any
    # same-named app-local module.
    env = backend.get("environment", {})
    assert env.get("PYTHONPATH") == "/python_core:/app"


def test_typescript_service_mounts_core_typescript_and_sets_node_path():
    arch = _arch(_svc("frontend", "frontend", "react", "typescript",
                      port=5173, skeleton="react"))
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    frontend = parsed["services"]["frontend"]
    assert "../../core/typescript:/ts_core" in frontend["volumes"]
    env = frontend.get("environment", {})
    assert env.get("NODE_PATH") == "/ts_core"


def test_javascript_service_treated_like_typescript():
    # Workers + small services sometimes use plain JS — same mount.
    arch = _arch(_svc("worker", "worker", "node", "javascript",
                      port=None, skeleton="none"))
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    worker = parsed["services"]["worker"]
    assert "../../core/typescript:/ts_core" in worker["volumes"]


def test_non_python_non_ts_service_does_not_get_core_mount():
    # Database / cache services don't need the shared core mounted —
    # they're not running app code that would import from it.
    # build_compose only emits app-service entries for _APP_SERVICE_TYPES,
    # so we test that infrastructure services (template-provided) skip
    # the core mount.
    arch = _arch(_svc("db", "database", "postgres", "sql", port=5432))
    out = TemplateOutput(
        compose_service={
            "image": "postgres:16-alpine",
            "ports": ["5432:5432"],
            "volumes": [],
        },
    )
    yml = build_compose(
        arch, template_outputs={"db": out}, project_slug="x",
    )
    parsed = yaml.safe_load(yml)
    db = parsed["services"]["db"]
    # Template-provided service has only the volumes the template
    # declared — no core mount injected.
    assert all("core/" not in v for v in db.get("volumes", []))


def test_template_provided_compose_used_when_present():
    arch = _arch(_svc("redis", "cache", "redis", "yaml", port=6380))
    out = TemplateOutput(
        compose_service={
            "image": "redis:7-alpine",
            "ports": ["6380:6379"],
        },
        compose_networks=["app-network"],
    )
    yml = build_compose(arch, template_outputs={"redis": out}, project_slug="x")
    parsed = yaml.safe_load(yml)
    assert parsed["services"]["redis"]["image"] == "redis:7-alpine"
    assert "6380:6379" in parsed["services"]["redis"]["ports"]


def test_volumes_aggregated_from_template_outputs():
    arch = _arch(_svc("postgres", "database", "postgres", "sql", port=5433))
    out = TemplateOutput(
        compose_service={"image": "postgres:16-alpine"},
        compose_volumes=["pgdata"],
    )
    yml = build_compose(arch, template_outputs={"postgres": out}, project_slug="x")
    parsed = yaml.safe_load(yml)
    assert "pgdata" in parsed["volumes"]


def test_dependency_uses_service_healthy_for_db():
    arch = _arch(
        _svc("postgres", "database", "postgres", "sql", port=5433),
        _svc("backend", "backend", "fastapi", "python", port=8001, depends_on=["postgres"], skeleton="fastapi"),
    )
    pg_out = TemplateOutput(
        compose_service={"image": "postgres:16-alpine"},
    )
    yml = build_compose(arch, template_outputs={"postgres": pg_out}, project_slug="x")
    parsed = yaml.safe_load(yml)
    backend_deps = parsed["services"]["backend"]["depends_on"]
    assert backend_deps["postgres"]["condition"] == "service_healthy"


def test_dependency_uses_service_started_for_app():
    arch = _arch(
        _svc("backend", "backend", "fastapi", "python", port=8001, skeleton="fastapi"),
        _svc("frontend", "frontend", "react", "typescript", port=5173, depends_on=["backend"], skeleton="react"),
    )
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    frontend_deps = parsed["services"]["frontend"]["depends_on"]
    assert frontend_deps["backend"]["condition"] == "service_started"


def test_unknown_dependency_is_dropped():
    arch = _arch(
        _svc("backend", "backend", "fastapi", "python", port=8001, depends_on=["ghost"], skeleton="fastapi"),
    )
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    assert "depends_on" not in parsed["services"]["backend"]


def test_react_uses_5173_container_port():
    arch = _arch(_svc("frontend", "frontend", "react", "typescript", port=5174, skeleton="react"))
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    assert "5174:5173" in parsed["services"]["frontend"]["ports"]


def test_angular_uses_4200_container_port():
    arch = _arch(_svc("dashboard", "frontend", "angular", "typescript", port=4201, skeleton="angular"))
    yml = build_compose(arch, template_outputs={}, project_slug="x")
    parsed = yaml.safe_load(yml)
    assert "4201:4200" in parsed["services"]["dashboard"]["ports"]

"""
Example: Auto Architect

Decomposes a problem into a service-based architecture,
creates project structure with Dockerfiles and docker-compose.yml,
builds Docker images, and dispatches Engineer instances.

Output structure:
    project_root/
    ├── backend/                  (service source code)
    ├── frontend/                 (service source code)
    └── infra/
        └── development/
            ├── docker-compose.yml
            ├── .env
            ├── backend/          (Dockerfile)
            └── frontend/         (Dockerfile)

Requirements:
    - OPENAI_API_KEY environment variable set (or .env file)
    - Docker daemon running
"""
import os
import sys
import time
import subprocess
from pathlib import Path

# Force unbuffered output so progress is visible in real time
os.environ["PYTHONUNBUFFERED"] = "1"

from dotenv import load_dotenv

load_dotenv()

from bizniz.agents.coder.coder import Coder
from bizniz.agents.debugger.quick import QuickDebugger
from bizniz.agents.debugger.agentic import AgenticDebugger
from bizniz.integration.http_api_tester import HTTPApiTester
from bizniz.integration.web_ui_tester import WebUITester
from bizniz.tester.tester import Tester
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.environment.docker_environment import DockerExecutionEnvironment
from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
from bizniz.environment.docker_jest_environment import DockerJestEnvironment
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.engineer.engineer import Engineer
from bizniz.architect.architect import Architect
from bizniz.workspace.local_workspace import LocalWorkspace


PROBLEM_STATEMENT = (
    "Build a web application for a pet grooming salon. "
    "The website should allow customers to: "
    "1) View available grooming services (bath, haircut, nail trim, etc.) with prices, "
    "2) Book an appointment by selecting a service, date, and time slot, "
    "3) View and cancel their existing appointments. "
    "\n\n"
    "The backend should be a REST API with endpoints for services, appointments, "
    "and basic validation (no double-booking the same time slot). "
    "Use in-memory storage for now (no database required)."
)


_start_time = time.time()


def log(msg: str):
    """Timestamped, flush-safe logging."""
    elapsed = time.time() - _start_time
    print(f"  [{elapsed:6.1f}s] {msg}", flush=True)


def preflight_checks():
    """Validate prerequisites before running the pipeline."""
    errors = []

    # Check API key — bizniz.yaml drives which provider; we accept any
    # of the supported ones being set.
    has_any = any(
        os.environ.get(k)
        for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY")
    )
    if not has_any:
        errors.append(
            "No AI provider key set — need one of OPENAI_API_KEY, "
            "GEMINI_API_KEY, or ANTHROPIC_API_KEY (export or in .env)."
        )

    # Check Docker daemon
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            errors.append("Docker daemon is not running. Start Docker and try again.")
    except FileNotFoundError:
        errors.append("Docker is not installed. Install Docker and try again.")
    except subprocess.TimeoutExpired:
        errors.append("Docker daemon is not responding (timed out).")

    if errors:
        print("\n  PREFLIGHT FAILED:", flush=True)
        for err in errors:
            print(f"    - {err}", flush=True)
        print(flush=True)
        sys.exit(1)

    log("Preflight OK (API key set, Docker running)")


def _make_orchestrator(config, workspace, on_status_message=None, suggested_model=None, image_name=None, language="python"):
    sandbox = DockerExecutionEnvironment()

    if language == "typescript":
        test_env = DockerJestEnvironment(
            workspace_root=workspace.root,
            image=image_name or "bizniz-node-runner",
        )
    else:
        test_env = DockerPytestEnvironment(
            workspace_root=workspace.root,
            image=image_name or "bizniz-python-runner",
        )

    def debugger_factory():
        fresh_client = config.make_client()
        return AgenticDebugger(
            client=fresh_client, workspace=workspace, environment=test_env,
            on_status_message=on_status_message,
        )

    def client_factory(model_name):
        return config.make_client(model=model_name)

    issue_client = config.make_client(model=suggested_model) if suggested_model else config.make_client()

    return CodingOrchestrator(
        coder=Coder(client=issue_client, environment=sandbox, workspace=workspace),
        tester=Tester(client=issue_client, environment=sandbox, workspace=workspace),
        quick_debugger=QuickDebugger(client=issue_client, environment=sandbox, workspace=workspace),
        test_environment=test_env,
        workspace=workspace,
        client=issue_client,
        client_factory=client_factory,
        debugger_factory=debugger_factory,
        model_progression=config.make_model_progression(),
        coder_progression=config.make_autocoder_progression(),
        tester_progression=config.make_autotester_progression(),
        repair_progression=config.make_repair_progression(),
        stall_threshold=config.stall_threshold,
        agentic_debug_threshold=config.agentic_debug_threshold,
        max_iterations=config.max_iterations,
        on_status_message=on_status_message,
        language=language,
        enable_agentic_debug=config.enable_agentic_debug,
        stall_recovery=config.stall_recovery,
    )


def _make_engineer(config, workspace, on_status_message=None, image_name=None, language="python"):
    def orchestrator_factory(suggested_model=None):
        return _make_orchestrator(
            config, workspace,
            on_status_message=on_status_message,
            suggested_model=suggested_model,
            image_name=image_name,
            language=language,
        )

    engineer_client = config.make_engineer_client()

    return Engineer(
        client=engineer_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        orchestrator_factory=orchestrator_factory,
        on_status_message=on_status_message,
        language=language,
        available_models=config.coder_models or config.models,
        debugger_model=config.debugger_model,
        debugger_max_iterations=config.debugger_max_iterations,
    )


if __name__ == "__main__":

    no_skeleton = "--no-skeleton" in sys.argv

    print(f"\n{'='*60}", flush=True)
    print(f"  Auto Architect{' (NO SKELETON)' if no_skeleton else ''}", flush=True)
    print(f"{'='*60}\n", flush=True)

    preflight_checks()

    log("Loading config...")
    config = BiznizConfig.find_and_load()
    log(f"Config: default_model={config.default_model}, engineer_model={config.engineer_model}, architect_model={config.architect_model}")
    log(f"Model progression: {config.models}")
    log(f"Coder models: {config.coder_models or config.models}")
    log(f"Repair models: {config.repair_models or config.models}")
    log(f"Stall threshold: {config.stall_threshold}, Agentic debug threshold: {config.agentic_debug_threshold}")
    log(f"Layered generation: {config.layered_generation}, Parallel services: {config.parallel_services}")

    log("Creating architect client...")
    architect_client = config.make_client(model=config.architect_model)
    log(f"Architect client ready (model={config.architect_model})")

    project_name = "Pet Groomer NoSkel" if no_skeleton else "Pet Groomer V11"
    project_parent = Path.home() / "bizniz_projects"
    project_parent.mkdir(parents=True, exist_ok=True)

    root_workspace = LocalWorkspace.from_name(project_name, parent=project_parent)

    def _make_http_api_tester(workspace):
        return HTTPApiTester(
            client=config.make_engineer_client(),
            environment=PythonSandboxExecutionEnvironment(),
            workspace=workspace,
            on_status_message=log,
        )

    def _make_integration_debugger(workspace):
        # Defaults: 15 inner tool-call turns × 3 outer repair
        # iterations = up to 45 debugger interactions per failing
        # service.
        return AgenticDebugger(
            client=config.make_client(model=config.debugger_model),
            workspace=workspace,
            environment=PythonSandboxExecutionEnvironment(),
            on_status_message=log,
        )

    def _make_web_ui_tester(workspace):
        return WebUITester(
            client=config.make_engineer_client(),
            environment=PythonSandboxExecutionEnvironment(),
            workspace=workspace,
            on_status_message=log,
        )

    def _make_ux_designer_kwargs():
        """Return kwargs dict for run_ux_review (vision_client + coder_factory)."""
        from bizniz.clients.gemini.gemini_client import GeminiClient
        vision_client = GeminiClient(model_name="gemini-flash")
        def _coder_for_ux(workspace):
            from bizniz.agents.coder.coder import Coder
            return Coder(
                client=config.make_client(),
                environment=DockerExecutionEnvironment(),
                workspace=workspace,
            )
        return {
            "vision_client": vision_client,
            "coder_factory": _coder_for_ux,
        }

    architect = Architect(
        client=architect_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=root_workspace,
        engineer_factory=lambda ws, on_status_message=None, image_name=None, language="python": _make_engineer(
            config, ws, on_status_message=on_status_message, image_name=image_name, language=language,
        ),
        http_api_tester_factory=lambda workspace: _make_http_api_tester(workspace),
        integration_debugger_factory=lambda workspace: _make_integration_debugger(workspace),
        web_ui_tester_factory=lambda workspace: _make_web_ui_tester(workspace),
        ux_designer_factory=_make_ux_designer_kwargs,
        project_parent=str(project_parent),
        on_status_message=log,
    )

    log("Starting build pipeline...")

    try:
        result = architect.build(
            PROBLEM_STATEMENT, project_name,
            parallel=config.parallel_services,
            max_workers=config.max_service_workers,
            layered=config.layered_generation,
            force_no_skeleton=no_skeleton,
        )
    except KeyboardInterrupt:
        log("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        log(f"PIPELINE FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print(f"\n{'='*60}", flush=True)
    print(f"  Results", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  Project: {result.project_name}", flush=True)
    print(f"  Project root: {result.project_root}", flush=True)
    print(f"  Services: {len(result.architecture.services)}", flush=True)
    print(f"  Docker compose: {result.docker_compose_path}", flush=True)
    print(flush=True)
    for sr in result.service_results:
        status_str = "PASS" if sr.success else "FAIL"
        print(f"  {sr.service_name}: {status_str} ({sr.issues_passed}/{sr.issues_total} issues)", flush=True)

    elapsed = time.time() - _start_time
    print(f"\n  Total elapsed: {elapsed:.0f}s", flush=True)

    # Cost summary for this run
    try:
        from bizniz.cost import get_tracker
        cost = get_tracker().summary()
        print(flush=True)
        print(f"{'='*60}", flush=True)
        print(f"  Cost", flush=True)
        print(f"{'='*60}", flush=True)
        print("  " + cost.format().replace("\n", "\n  "), flush=True)
    except Exception as e:
        print(f"  Cost summary unavailable: {e}", flush=True)
    print(flush=True)

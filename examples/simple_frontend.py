"""
Example: Simple Frontend App

Creates a single-service TypeScript/React frontend app using the Architect
pipeline. Used for testing and debugging TypeScript support end-to-end.

Requirements:
    - OPENAI_API_KEY environment variable set (or .env file)
    - Docker daemon running
"""
import os
import sys
import time
import subprocess
from pathlib import Path

os.environ["PYTHONUNBUFFERED"] = "1"

from dotenv import load_dotenv

load_dotenv()

from bizniz.agents.coder.coder import Coder
from bizniz.agents.debugger.quick import QuickDebugger
from bizniz.agents.debugger.agentic import AgenticDebugger
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
    "Build a simple single-page counter app using TypeScript. "
    "The app should have: "
    "1) A counter display showing the current count (starting at 0), "
    "2) An increment button that adds 1 to the counter, "
    "3) A decrement button that subtracts 1 from the counter, "
    "4) A reset button that sets the counter back to 0. "
    "\n\n"
    "This is a simple TypeScript app with pure DOM manipulation (no React). "
    "Use a single TypeScript file for the logic."
)


_start_time = time.time()


def log(msg: str):
    elapsed = time.time() - _start_time
    print(f"  [{elapsed:6.1f}s] {msg}", flush=True)


def preflight_checks():
    errors = []
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        errors.append("OPENAI_API_KEY environment variable is not set.")

    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            errors.append("Docker daemon is not running.")
    except FileNotFoundError:
        errors.append("Docker is not installed.")
    except subprocess.TimeoutExpired:
        errors.append("Docker daemon is not responding.")

    if errors:
        print("\n  PREFLIGHT FAILED:", flush=True)
        for err in errors:
            print(f"    - {err}", flush=True)
        sys.exit(1)

    log("Preflight OK")


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
        fresh_client = config.make_client(model=config.debugger_model)
        return AgenticDebugger(
            client=fresh_client, workspace=workspace, environment=test_env,
            on_status_message=on_status_message,
        )

    def client_factory(model_name):
        return config.make_client(model=model_name)

    issue_client = config.make_client(model=suggested_model or config.engineer_model)

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
        max_iterations=config.max_iterations,
        on_status_message=on_status_message,
        language=language,
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
    )


if __name__ == "__main__":

    print(f"\n{'='*60}", flush=True)
    print(f"  Simple Frontend App (TypeScript pipeline test)", flush=True)
    print(f"{'='*60}\n", flush=True)

    preflight_checks()

    log("Loading config...")
    config = BiznizConfig.find_and_load()
    log(f"Config: engineer_model={config.engineer_model}, max_iterations={config.max_iterations}")

    log("Creating architect client...")
    architect_client = config.make_client(model="gpt-4o")

    project_name = "Simple Counter"
    project_parent = Path.home() / "bizniz_projects"
    project_parent.mkdir(parents=True, exist_ok=True)

    root_workspace = LocalWorkspace.from_name(project_name, parent=project_parent)

    architect = Architect(
        client=architect_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=root_workspace,
        engineer_factory=lambda ws, on_status_message=None, image_name=None, language="python": _make_engineer(
            config, ws, on_status_message=on_status_message, image_name=image_name, language=language,
        ),
        project_parent=str(project_parent),
        on_status_message=log,
    )

    log("Starting build pipeline...")

    try:
        result = architect.build(PROBLEM_STATEMENT, project_name)
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
    print(flush=True)

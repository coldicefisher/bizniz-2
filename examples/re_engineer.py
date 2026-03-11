#!/usr/bin/env python3
"""
Re-run AutoEngineer analysis only (no code gen).

Clears existing issues for a given problem_id and re-analyzes
with updated prompts. Used to test prompt changes without full blast.

Usage:
    PYTHONUNBUFFERED=1 python3 examples/re_engineer.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────

PROJECT_ROOT = Path("/home/jamey/bizniz_projects/pet_groomer")
WORKSPACE_DIR = PROJECT_ROOT / "backend"
PROBLEM_ID = 8  # Fresh issues with test_setup_hint + dependencies


def main():
    from bizniz.workspace.local_workspace import LocalWorkspace
    from bizniz.clients.chatgpt.openai_chatgpt_client import OpenAIChat4GPTClient
    from bizniz.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig
    from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
    from bizniz.autocoder.autocoder import Autocoder
    from bizniz.autotester.autotester import Autotester
    from bizniz.engineer.auto_engineer import AutoEngineer

    print("=" * 60)
    print("  Re-Engineer: Fresh issue decomposition")
    print("=" * 60)

    workspace = LocalWorkspace(root=WORKSPACE_DIR)

    model = "gpt-5"
    client_config = ChatGPTClientConfig(default_model=model)
    client = OpenAIChat4GPTClient(
        config=client_config,
        api_key=os.environ["OPENAI_API_KEY"],
    )

    env = DockerPytestEnvironment(
        workspace_root=WORKSPACE_DIR,
        image="pet_groomer-backend:dev",
    )

    def make_orchestrator():
        autocoder = Autocoder(client=client, environment=env, workspace=workspace)
        autotester = Autotester(client=client, environment=env, workspace=workspace)
        return CodingOrchestrator(
            autocoder=autocoder,
            autotester=autotester,
            test_environment=env,
            workspace=workspace,
        )

    def status(msg):
        print(f"  {msg}")

    engineer = AutoEngineer(
        client=client,
        environment=env,
        workspace=workspace,
        orchestrator_factory=make_orchestrator,
        on_status_message=status,
        language="python",
        available_models=["gpt-4o-mini", "gpt-4o"],
    )

    # Problem statement (same as problem 4)
    problem_statement = """Overall project: Build a web application for a pet grooming salon. The website should allow customers to: 1) View available grooming services (bath, haircut, nail trim, etc.) with prices, 2) Book an appointment by selecting a service, date, and time slot, 3) View and cancel their existing appointments.

The backend should be a REST API with endpoints for services, appointments, and basic validation (no double-booking the same time slot). Use in-memory storage for now (no database required).

You are building the 'backend' service for the 'Pet Groomer' project.

Service details:
- Type: backend
- Framework: fastapi
- Language: python
- Description: FastAPI REST API providing endpoints for grooming services, appointment booking, listing, and cancellation. Enforces no double-booking of time slots. Uses in-memory storage.
- Port: 8000

Other services in the system:
- frontend (react): React + TypeScript single-page app for customers to view services, book appointments, and manage existing bookings. Talks to the backend REST API.

Build ONLY this service. Use python with fastapi. Focus on clean, working code with tests. The service will run in a Docker container."""

    print(f"\n  Running analysis with problem_id={PROBLEM_ID}...")
    print(f"  Model: {model}")
    print()

    analysis = engineer.analyze(problem_statement)

    # Print results
    print(f"\n{'=' * 60}")
    print(f"  ANALYSIS RESULTS")
    print(f"{'=' * 60}")
    print(f"  Requirements: {len(analysis.requirements)}")
    print(f"  Use cases: {len(analysis.use_cases)}")
    print(f"  Issues: {len(analysis.issues)}")

    if analysis.architecture:
        plan = analysis.architecture
        print(f"\n  Architecture: {plan.package_name}")
        print(f"  Namespaces: {len(plan.namespaces)}")
        print(f"  Domain models: {len(plan.domain_models)}")
        print(f"  Modules: {len(plan.modules)}")

    print(f"\n{'─' * 60}")
    print(f"  ISSUES (topological order):")
    print(f"{'─' * 60}")

    for i, issue in enumerate(analysis.issues):
        deps = issue.depends_on_titles
        dep_str = f" → depends on: {deps}" if deps else ""
        targets = [tf.filepath for tf in issue.target_files]
        print(f"\n  {i+1}. [{issue.db_id}] {issue.title}{dep_str}")
        print(f"     Description: {issue.description[:120]}...")
        print(f"     Files: {', '.join(targets)}")
        print(f"     Tests: {', '.join(issue.test_files)}")
        print(f"     Model: {issue.suggested_model}")
        if issue.test_setup_hint:
            print(f"     Test setup: {issue.test_setup_hint[:120]}...")

    # Save for reference
    docs_dir = PROJECT_ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    issues_path = docs_dir / "issues_v8.json"
    issues_data = []
    for issue in analysis.issues:
        issues_data.append({
            "id": issue.db_id,
            "title": issue.title,
            "description": issue.description,
            "target_files": [{"filepath": tf.filepath, "action": tf.action} for tf in issue.target_files],
            "test_files": issue.test_files,
            "depends_on": issue.depends_on_titles,
            "suggested_model": issue.suggested_model,
            "test_setup_hint": issue.test_setup_hint or "",
        })
    with open(issues_path, "w") as f:
        json.dump(issues_data, f, indent=2)
    print(f"\n  Issues saved to {issues_path}")

    env.stop()


if __name__ == "__main__":
    main()

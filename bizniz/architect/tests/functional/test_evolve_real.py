"""Functional test for Architect.evolve() against real Gemini.

Verifies the AI can take an existing architecture + a new milestone
and return a sensible delta (existing services preserved, new ones
added, evolve_state populated correctly).

Marked ``functional`` — skipped when GEMINI_API_KEY isn't set.
"""
import os

import pytest

from bizniz.architect.architect import Architect
from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.planner.types import Milestone
from bizniz.workspace.local_workspace import LocalWorkspace


pytestmark = pytest.mark.functional


def _ensure_keys():
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set — skipping functional test")


def _milestone1_arch():
    """Existing 'auth + profile' state — postgres + fusionauth + backend."""
    return SystemArchitecture(
        project_name="Notes App",
        project_slug="notes_app",
        description="Notes app, milestone 1: auth + profile",
        services=[
            ServiceDefinition(
                name="postgres", service_type="database", framework="postgres",
                language="sql", description="primary db",
                workspace_name="postgres", port=5433,
                depends_on=[], requirements=[], skeleton="none",
                evolve_state="unchanged",
            ),
            ServiceDefinition(
                name="auth", service_type="auth", framework="fusionauth",
                language="yaml", description="oauth provider",
                workspace_name="fusionauth", port=9011,
                depends_on=["postgres"], requirements=[], skeleton="none",
                evolve_state="unchanged",
            ),
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="REST API",
                workspace_name="backend", port=8001,
                depends_on=["postgres", "auth"],
                requirements=["fastapi", "pydantic"],
                skeleton="fastapi",
                evolve_state="unchanged",
            ),
            ServiceDefinition(
                name="frontend", service_type="frontend", framework="react",
                language="typescript", description="React SPA",
                workspace_name="frontend", port=5174,
                depends_on=["backend"], requirements=[],
                skeleton="react",
                evolve_state="unchanged",
            ),
        ],
    )


def test_evolve_adds_notes_to_existing_project(tmp_path):
    """Real Gemini: extend the auth+profile project with note-taking.
    The AI should keep the 4 existing services and either extend backend
    + frontend (most likely) or add a new service. Either is acceptable."""
    _ensure_keys()
    config = BiznizConfig.find_and_load()
    architect_client = config.make_client(model=config.architect_model)
    workspace = LocalWorkspace(root=tmp_path / "_arch_workspace")

    architect = Architect(
        client=architect_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        engineer_factory=lambda *a, **kw: None,
    )

    milestone = Milestone(
        sequence_index=1,
        name="Personal notes CRUD",
        problem_slice=(
            "Authenticated users can create, view, edit, and delete their "
            "own notes. Each note has a title and body. Users only see "
            "their own notes."
        ),
        use_cases=[
            "user can create a note",
            "user can view their list of notes",
            "user can edit a note",
            "user can delete a note",
        ],
        success_criteria=[
            "user only sees their own notes",
            "deleted notes disappear from the list",
        ],
        depends_on_names=["Auth + profile"],
        estimated_effort="M",
    )

    existing = _milestone1_arch()
    evolved = architect.evolve(
        milestone=milestone,
        existing_architecture=existing,
        problem_statement="Build a notes app with auth and personal note management.",
        project_name="Notes App",
    )

    by_name = {s.name: s for s in evolved.services}

    # All existing services preserved
    for name in ("postgres", "auth", "backend", "frontend"):
        assert name in by_name, f"existing service {name} dropped by evolve"

    # Existing services keep their identity
    assert by_name["backend"].framework == "fastapi"
    assert by_name["backend"].port == 8001
    assert by_name["frontend"].framework == "react"

    # At least one service should be tagged "extended" or "new" — the
    # milestone genuinely adds work, the AI should not return everything
    # as "unchanged".
    states = {s.evolve_state for s in evolved.services}
    assert states & {"extended", "new"}, (
        f"Expected at least one extended or new service. States: "
        f"{[(s.name, s.evolve_state) for s in evolved.services]}"
    )

    # The backend or a new worker is the natural place for note CRUD.
    # If backend is unchanged, there must be a new service to handle notes.
    if by_name["backend"].evolve_state == "unchanged":
        new_or_extended = [s for s in evolved.services
                           if s.evolve_state in ("new", "extended") and s.name != "backend"]
        assert new_or_extended, (
            "backend is unchanged but no other service handles notes — "
            "the milestone needs SOMEONE to deliver note CRUD"
        )

    # Infrastructure should remain unchanged (postgres + auth)
    assert by_name["postgres"].evolve_state in ("unchanged", "extended")
    assert by_name["auth"].evolve_state in ("unchanged", "extended")

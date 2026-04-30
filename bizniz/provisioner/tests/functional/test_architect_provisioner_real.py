"""
End-to-end functional test for architect + provisioner.

Runs the real architect (network call to Gemini) on a CRM-shaped problem
and asserts that the Provisioner produces the expected on-disk layout.
Does NOT build Docker images and does NOT dispatch engineers — pure
planning + materialization.

Marked ``functional`` so it is deselected by the default pytest run
(see ``bizniz/pytest.ini``). Run explicitly with::

    pytest -m functional bizniz/provisioner/tests/functional/

Skipped automatically when ``GEMINI_API_KEY`` is not set so a fresh
checkout doesn't crash the suite.
"""
import os
from pathlib import Path

import pytest

from bizniz.architect.architect import Architect
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.provisioner import Provisioner
from bizniz.workspace.local_workspace import LocalWorkspace


CRM_PROBLEM = (
    "Build a small CRM web application. "
    "Customers can sign up and log in (OAuth). "
    "Authenticated users can manage contacts (CRUD), companies (CRUD), and "
    "deals attached to a contact. "
    "Backend exposes a REST API. Frontend is a single-page app. "
    "Use a relational database for persistence."
)


pytestmark = pytest.mark.functional


def _ensure_keys():
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set — skipping functional test")


def test_architect_plans_crm_with_fusionauth(tmp_path, monkeypatch):
    """Architect should plan an auth service when the problem mentions login.

    Uses ``Provisioner(build_images=False)`` so the test stays cheap and
    doesn't require Docker.
    """
    _ensure_keys()
    monkeypatch.chdir(tmp_path)

    config = BiznizConfig.find_and_load()
    architect_client = config.make_client(model=config.architect_model)

    workspace = LocalWorkspace(root=tmp_path / "_arch_workspace")

    # Provide a Provisioner that doesn't build images.
    provisioner = Provisioner(
        project_parent=tmp_path,
        build_images=False,
    )

    architect = Architect(
        client=architect_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        engineer_factory=lambda *a, **kw: _NoopEngineerCM(),
        project_parent=str(tmp_path),
        provisioner=provisioner,
    )

    # Decompose only — assert the plan looks sensible.
    architecture = architect.decompose(CRM_PROBLEM, project_name="Mini CRM")
    service_names = {s.name for s in architecture.services}
    types = {s.service_type for s in architecture.services}
    frameworks = {s.framework for s in architecture.services}

    assert "backend" in {s.service_type for s in architecture.services}
    assert "frontend" in {s.service_type for s in architecture.services}

    # CRM has user accounts → auth must appear
    assert "auth" in types, (
        f"Expected an auth service for a CRM with login. Got types: {types}, "
        f"frameworks: {frameworks}"
    )
    # FusionAuth is the prompted default OAuth provider
    assert "fusionauth" in frameworks, (
        f"Expected fusionauth as auth framework. Got: {frameworks}"
    )
    # Database required by FusionAuth
    assert "database" in types or "postgres" in frameworks, (
        f"Expected a postgres database when FusionAuth is in play. "
        f"Got types: {types}, frameworks: {frameworks}"
    )


def test_full_architect_build_through_provisioner(tmp_path, monkeypatch):
    """Architect.build() runs end-to-end and the Provisioner lays out the
    project as expected. Engineer dispatch is stubbed so we don't burn
    credits on codegen."""
    _ensure_keys()
    monkeypatch.chdir(tmp_path)

    config = BiznizConfig.find_and_load()
    architect_client = config.make_client(model=config.architect_model)
    workspace = LocalWorkspace(root=tmp_path / "_arch_workspace")

    provisioner = Provisioner(project_parent=tmp_path, build_images=False)

    architect = Architect(
        client=architect_client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
        engineer_factory=lambda *a, **kw: _NoopEngineerCM(),
        project_parent=str(tmp_path),
        provisioner=provisioner,
    )

    result = architect.build(
        CRM_PROBLEM,
        project_name="Mini CRM",
        parallel=False,
        layered=False,
    )
    project_root = Path(result.project_root)

    # Top-level layout
    assert project_root.is_dir()
    assert (project_root / "infra" / "development" / "docker-compose.yml").is_file()
    assert (project_root / "infra" / "development" / ".env").is_file()

    # If FusionAuth was selected, kickstart and postgres init.sql exist
    fa_kickstart = project_root / "infra/development/fusionauth/kickstart/kickstart.json"
    pg_init = project_root / "infra/development/postgres/init.sql"
    auth_planned = any(s.framework == "fusionauth" for s in result.architecture.services)
    if auth_planned:
        assert fa_kickstart.is_file(), \
            "FusionAuth was planned but kickstart was not provisioned"
        assert pg_init.is_file(), \
            "FusionAuth requires postgres but init.sql was not provisioned"


# ── helpers ──────────────────────────────────────────────────────────────────

class _NoopEngineerCM:
    """Engineer factory stub — provides the context-manager surface plus
    analyze/dispatch/run_three_phase methods that immediately succeed.
    Keeps the architect happy without spending money on real codegen."""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def analyze(self, problem_statement):
        from bizniz.engineer.types import EngineeringAnalysis
        return EngineeringAnalysis(problem_id=0, requirements=[], use_cases=[], issues=[])

    def run_three_phase(self, problem_statement, analysis=None):
        return []

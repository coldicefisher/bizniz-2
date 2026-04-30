"""
Functional test for the Planner against real Gemini.

Verifies the Planner produces a sensible CRM plan: at least 3 milestones,
auth-shaped milestone first, contacts before deals, dependencies referenced
by name. Marked ``functional`` so it's skipped by the default suite.
"""
import os

import pytest

from bizniz.config.bizniz_config import BiznizConfig
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.planner import Planner
from bizniz.workspace.local_workspace import LocalWorkspace


CRM_PROBLEM = (
    "Build a CRM web application. "
    "Customers can sign up and log in. "
    "Authenticated users can manage contacts (add, edit, delete, view). "
    "Users can attach deals to contacts (deal name, value, stage). "
    "Users can organize deals into pipelines with custom stages. "
    "Users can view a reporting dashboard with deal-value totals and "
    "deals-per-stage charts."
)


pytestmark = pytest.mark.functional


def _ensure_keys():
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set — skipping functional test")


def test_plan_crm_real_gemini(tmp_path):
    _ensure_keys()
    config = BiznizConfig.find_and_load()
    client = config.make_planner_client()
    workspace = LocalWorkspace(root=tmp_path / "_planner_ws")

    planner = Planner(
        client=client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
    )
    plan = planner.plan(
        problem_statement=CRM_PROBLEM,
        project_name="Mini CRM",
    )

    # Plan must have at least 3 milestones (the prompt's lower bound)
    assert len(plan.milestones) >= 3, (
        f"Expected at least 3 milestones, got {len(plan.milestones)}: "
        f"{[m.name for m in plan.milestones]}"
    )

    # Sequenced
    assert [m.sequence_index for m in plan.milestones] == \
        list(range(len(plan.milestones)))

    # Each milestone has populated fields
    for m in plan.milestones:
        assert m.name and len(m.name) >= 3, f"Milestone {m.sequence_index} has no name"
        assert len(m.problem_slice) > 30, (
            f"Milestone {m.name!r} problem_slice too short: {m.problem_slice!r}"
        )
        assert m.use_cases, f"Milestone {m.name!r} has no use_cases"
        assert m.success_criteria, f"Milestone {m.name!r} has no success_criteria"
        assert m.estimated_effort in ("S", "M", "L"), (
            f"Milestone {m.name!r} bad effort: {m.estimated_effort!r}"
        )

    # Auth-shaped milestone should come early.
    names_lower = [m.name.lower() for m in plan.milestones]
    auth_idx = next(
        (i for i, n in enumerate(names_lower)
         if any(k in n for k in ("auth", "sign", "login", "log in"))),
        None,
    )
    assert auth_idx is not None, (
        f"Expected an auth-shaped milestone first. Got: {names_lower}"
    )
    assert auth_idx <= 1, (
        f"Auth milestone should be at index 0 or 1, found at {auth_idx}: "
        f"{names_lower}"
    )

    # If a "deals" milestone exists, it should depend on (or come after) a
    # contacts-shaped milestone.
    deals_milestones = [
        m for m in plan.milestones
        if "deal" in m.name.lower()
    ]
    contacts_milestones = [
        m for m in plan.milestones
        if any(k in m.name.lower() for k in ("contact", "person", "people"))
    ]
    if deals_milestones and contacts_milestones:
        deal = deals_milestones[0]
        contact = contacts_milestones[0]
        assert deal.sequence_index > contact.sequence_index, (
            f"Deals milestone (#{deal.sequence_index}) must come after "
            f"contacts (#{contact.sequence_index})"
        )

    # Dependencies should reference names that exist in this plan.
    plan_names = {m.name for m in plan.milestones}
    for m in plan.milestones:
        for dep_name in m.depends_on_names:
            assert dep_name in plan_names, (
                f"Milestone {m.name!r} depends on {dep_name!r} which isn't "
                f"in this plan. Plan names: {plan_names}"
            )


def test_plan_persists_to_db_real_gemini(tmp_path):
    """End-to-end: real plan + project DB persistence + rollup query."""
    _ensure_keys()
    from bizniz.project.project import Project

    config = BiznizConfig.find_and_load()
    client = config.make_planner_client()
    workspace = LocalWorkspace(root=tmp_path / "_planner_ws")
    project = Project(root=tmp_path / "proj", project_name="Mini CRM")
    project.create_structure()

    planner = Planner(
        client=client,
        environment=PythonSandboxExecutionEnvironment(),
        workspace=workspace,
    )
    plan = planner.plan(
        problem_statement="Build a simple notes app where users can sign up, log in, and CRUD their notes.",
        project_name="Notes",
        project_db=project.db,
    )

    assert plan.db_id is not None
    active = project.db.get_active_plan("notes")
    assert active is not None
    assert active["id"] == plan.db_id
    assert active["archived_at"] is None

    rows = project.db.get_milestones(plan.db_id)
    assert len(rows) == len(plan.milestones)
    assert all(r["status"] == "planned" for r in rows)

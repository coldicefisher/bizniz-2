"""Unit tests for the v2 Planner agent (mocked AI client).

The Planner is a single-call agent: one ``plan()`` call → one LLM
round-trip (modulo retries) → ``ProjectPlan`` out. No tools, no
re-plan path, no auth_delta.
"""
import json
from unittest.mock import MagicMock

import pytest

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.planner.planner import Planner
from bizniz.planner.types import PlannerBadAIResponseError
from bizniz.lib.llm_utils import LLMCallError
from bizniz.project.project import Project


# ── Fixtures ──────────────────────────────────────────────────────────────────

VALID_PLAN = {
    "project_name": "Mini CRM",
    "project_slug": "mini_crm",
    "description": "A simple CRM shipped in 4 phases.",
    "milestones": [
        {
            "sequence_index": 0,
            "name": "Auth + profile",
            "problem_slice": (
                "Build a CRM web app where users can sign up, log in, and "
                "view their own profile. Use OAuth/OIDC."
            ),
            "use_cases": [
                "user can sign up with email and password",
                "user can log in",
                "user can view their profile",
            ],
            "success_criteria": [
                "logged-in user sees their email on the profile page",
                "logged-out user is redirected to login",
            ],
            "depends_on_names": [],
            "estimated_effort": "M",
        },
        {
            "sequence_index": 1,
            "name": "Contact CRUD",
            "problem_slice": (
                "Authenticated users can add, view, edit, and delete contacts. "
                "A contact has name, email, phone, company."
            ),
            "use_cases": [
                "user can add a new contact",
                "user can view their contact list",
                "user can edit a contact",
                "user can delete a contact",
            ],
            "success_criteria": [
                "user sees only their own contacts",
                "deleted contacts disappear from the list",
            ],
            "depends_on_names": ["Auth + profile"],
            "estimated_effort": "L",
        },
        {
            "sequence_index": 2,
            "name": "Deals attached to contacts",
            "problem_slice": (
                "Add deals (name, value, stage) attached to contacts. User can "
                "create, edit, delete deals on a contact's page."
            ),
            "use_cases": [
                "user can attach a deal to a contact",
                "user can update deal stage",
                "user can see total deal value per contact",
            ],
            "success_criteria": [
                "deal value rolls up correctly on the contact page",
            ],
            "depends_on_names": ["Contact CRUD"],
            "estimated_effort": "M",
        },
    ],
}


def _ai_response(data):
    text = json.dumps(data)
    return text, "job-id", [{"role": "assistant", "content": text}]


@pytest.fixture
def mock_client():
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = _ai_response(VALID_PLAN)
    return client


@pytest.fixture
def project(tmp_path):
    p = Project(root=tmp_path / "proj", project_name="Mini CRM")
    p.create_structure()
    return p


# ── Planning ──────────────────────────────────────────────────────────────────

def test_plan_returns_ordered_milestones(mock_client):
    planner = Planner(client=mock_client)
    plan = planner.plan(
        problem_statement="Build a CRM",
        project_name="Mini CRM",
    )
    assert plan.project_slug == "mini_crm"
    assert len(plan.milestones) == 3
    assert plan.milestones[0].sequence_index == 0
    assert plan.milestones[0].name == "Auth + profile"
    assert plan.milestones[2].depends_on_names == ["Contact CRUD"]


def test_plan_milestones_sorted_by_sequence_index_even_when_ai_scrambles():
    """If the AI returns milestones in random order, the Planner stable-
    sorts them by sequence_index so downstream code can iterate safely."""
    scrambled = json.loads(json.dumps(VALID_PLAN))
    scrambled["milestones"] = [
        scrambled["milestones"][2],
        scrambled["milestones"][0],
        scrambled["milestones"][1],
    ]
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = _ai_response(scrambled)

    planner = Planner(client=client)
    plan = planner.plan(problem_statement="Build a CRM", project_name="Mini CRM")
    assert [m.sequence_index for m in plan.milestones] == [0, 1, 2]
    assert plan.milestones[0].name == "Auth + profile"


def test_plan_use_cases_and_success_criteria_preserved(mock_client):
    planner = Planner(client=mock_client)
    plan = planner.plan(problem_statement="Build a CRM", project_name="Mini CRM")
    m = plan.milestones[0]
    assert any("sign up" in uc for uc in m.use_cases)
    assert any("redirected to login" in sc for sc in m.success_criteria)


def test_plan_problem_slice_self_contained(mock_client):
    """problem_slice should be a standalone problem statement — long
    enough to be useful, doesn't reference other milestones by index."""
    planner = Planner(client=mock_client)
    plan = planner.plan(problem_statement="Build a CRM", project_name="Mini CRM")
    for m in plan.milestones:
        assert len(m.problem_slice) > 30
        assert "milestone 0" not in m.problem_slice.lower()
        assert "milestone 1" not in m.problem_slice.lower()


def test_plan_empty_milestones_raises():
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["milestones"] = []
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = _ai_response(bad)
    planner = Planner(client=client)
    with pytest.raises(PlannerBadAIResponseError):
        planner.plan(problem_statement="Build", project_name="X")


def test_plan_retries_on_empty_response():
    """If the AI returns an empty response, planner retries up to max_retries."""
    client = MagicMock(spec=BaseAIClient)
    client.get_text.side_effect = [
        ("", "job-id", []),                # empty first
        ("", "job-id", []),                # empty second
        _ai_response(VALID_PLAN),          # success on third
    ]
    planner = Planner(client=client, max_retries=3)
    plan = planner.plan(problem_statement="Build", project_name="X")
    assert len(plan.milestones) == 3
    assert client.get_text.call_count == 3


def test_plan_raises_after_max_retries():
    """All attempts empty → LLMCallError surfaces."""
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = ("", "job-id", [])
    planner = Planner(client=client, max_retries=2)
    with pytest.raises(LLMCallError):
        planner.plan(problem_statement="Build", project_name="X")


def test_plan_milestone_has_no_auth_delta_attribute():
    """v2: auth_delta is gone. The Planner doesn't decide identity state;
    the AuthAgent does. The Milestone model shouldn't carry it."""
    from bizniz.planner.types import Milestone
    m = Milestone(name="x", problem_slice="y")
    assert not hasattr(m, "auth_delta")


# ── Persistence ───────────────────────────────────────────────────────────────

def test_plan_persists_to_project_db(mock_client, project):
    planner = Planner(client=mock_client)
    plan = planner.plan(
        problem_statement="Build a CRM",
        project_name="Mini CRM",
        project_db=project.db,
    )
    assert plan.db_id is not None

    active = project.db.get_active_plan("mini_crm")
    assert active is not None
    assert active["id"] == plan.db_id
    assert active["archived_at"] is None

    rows = project.db.get_milestones(plan.db_id)
    assert len(rows) == 3
    assert rows[0]["name"] == "Auth + profile"
    assert rows[0]["sequence_index"] == 0
    assert json.loads(rows[0]["use_cases_json"])
    assert rows[2]["name"] == "Deals attached to contacts"
    assert json.loads(rows[2]["depends_on_json"]) == ["Contact CRUD"]


def test_replan_archives_prior_active_plan(mock_client, project):
    """Two consecutive plan() calls on the same project: the second
    archives the first so get_active_plan returns the new one."""
    planner = Planner(client=mock_client)
    p1 = planner.plan("Build a CRM", "Mini CRM", project_db=project.db)

    new_plan = json.loads(json.dumps(VALID_PLAN))
    new_plan["description"] = "v2 plan"
    mock_client.get_text.return_value = _ai_response(new_plan)
    p2 = planner.plan("Build a CRM with reporting", "Mini CRM", project_db=project.db)

    assert p1.db_id != p2.db_id
    active = project.db.get_active_plan("mini_crm")
    assert active["id"] == p2.db_id

    old = project.db._conn.execute(
        "SELECT * FROM project_plans WHERE id = ?", (p1.db_id,),
    ).fetchone()
    assert old["archived_at"] is not None


def test_plan_milestone_status_defaults_to_planned(mock_client, project):
    planner = Planner(client=mock_client)
    plan = planner.plan("X", "X", project_db=project.db)
    rows = project.db.get_milestones(plan.db_id)
    assert all(r["status"] == "planned" for r in rows)


def test_update_milestone_status_transitions(mock_client, project):
    planner = Planner(client=mock_client)
    plan = planner.plan("X", "X", project_db=project.db)
    m_id = plan.milestones[0].db_id

    project.db.update_milestone_status(m_id, "in_progress")
    row = project.db.get_milestone(m_id)
    assert row["status"] == "in_progress"
    assert row["started_at"] is not None
    assert row["completed_at"] is None

    project.db.update_milestone_status(m_id, "completed")
    row = project.db.get_milestone(m_id)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None


def test_get_milestones_filters_by_status(mock_client, project):
    planner = Planner(client=mock_client)
    plan = planner.plan("X", "X", project_db=project.db)
    project.db.update_milestone_status(plan.milestones[0].db_id, "completed")
    completed = project.db.get_milestones(plan.db_id, status="completed")
    planned = project.db.get_milestones(plan.db_id, status="planned")
    assert len(completed) == 1
    assert len(planned) == 2


def test_invalid_milestone_status_is_ignored(mock_client, project):
    planner = Planner(client=mock_client)
    plan = planner.plan("X", "X", project_db=project.db)
    m_id = plan.milestones[0].db_id
    # "cancelled" is not a valid milestone status — should be a no-op
    project.db.update_milestone_status(m_id, "cancelled")
    row = project.db.get_milestone(m_id)
    assert row["status"] == "planned"


def test_cost_by_milestone_aggregates(mock_client, project):
    """api_calls tagged with milestone_id roll up correctly."""
    from bizniz.cost.tracker import CallRecord
    from bizniz.cost.pricing import CallCost

    planner = Planner(client=mock_client)
    plan = planner.plan("X", "X", project_db=project.db)
    m_first = plan.milestones[0].db_id
    m_second = plan.milestones[1].db_id

    project.db.start_job("job-1", "x")

    def _rec(milestone_id, total):
        return CallRecord(
            timestamp="t", agent="a", model="gemini-flash",
            input_tokens=100, output_tokens=50, duration_ms=0,
            cost=CallCost(0, 0, total, "gemini-flash", True),
            milestone_id=milestone_id, job_id="job-1",
        )

    project.db.save_api_call(_rec(m_first, 0.10))
    project.db.save_api_call(_rec(m_first, 0.05))
    project.db.save_api_call(_rec(m_second, 0.20))

    rollup = {row["milestone_id"]: row for row in project.db.cost_by_milestone(plan.db_id)}
    assert rollup[m_first]["calls"] == 2
    assert rollup[m_first]["total_cost"] == pytest.approx(0.15)
    assert rollup[m_second]["calls"] == 1
    assert rollup[m_second]["total_cost"] == pytest.approx(0.20)
    third = plan.milestones[2].db_id
    assert rollup[third]["calls"] == 0
    assert rollup[third]["total_cost"] == 0.0

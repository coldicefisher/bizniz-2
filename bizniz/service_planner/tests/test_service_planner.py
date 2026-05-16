"""ServicePlanner tests — single-call agent that emits Issue lists."""
import json
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.code_reviewer.types import (
    CodeReviewReport, FlaggedSymbol,
)
from bizniz.coder.types import Issue
from bizniz.quality_engineer.types import (
    CapabilitySpec, CoverageReport, EnrichedSpec, Field as SpecField,
    MissingScenario,
)
from bizniz.service_planner.agent import ServicePlanner, ServicePlannerError


# ── Fixtures ───────────────────────────────────────────────────────────


def _service():
    return ServiceDefinition(
        name="backend", service_type="backend", framework="fastapi",
        language="python", description="API",
        workspace_name="backend", port=8000, depends_on=[],
        skeleton="fastapi",
    )


def _arch():
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=[_service()],
    )


def _spec():
    return EnrichedSpec(
        milestone_name="M1",
        capabilities=[
            CapabilitySpec(
                id="create_pet", name="Create pet",
                description="Create a pet record",
                inputs=[SpecField(name="name", type="string", required=True)],
                outputs=[], validation_rules=[], error_cases=[],
                edge_cases=[], auth_required=True, allowed_roles=["user"],
                test_scenarios=["happy"],
            ),
        ],
    )


_SENTINEL = object()


def _issue_dict(id_, deps=None, files=_SENTINEL, tests=_SENTINEL, refs=None):
    if files is _SENTINEL:
        files = [f"app/{id_.lower()}.py"]
    if tests is _SENTINEL:
        tests = [f"tests/test_{id_.lower()}.py"]
    return {
        "id": id_,
        "title": f"Implement {id_}",
        "description": f"desc for {id_}",
        "target_files": files,
        "test_files": tests,
        "success_criteria": ["compiles"],
        "spec_refs": refs or ["create_pet"],
        "depends_on": deps or [],
    }


def _client_returning(payload: dict) -> BaseAIClient:
    c = MagicMock(spec=BaseAIClient)
    c.get_text.return_value = (json.dumps(payload), "j", [])
    return c


# ── Happy path ─────────────────────────────────────────────────────────


class TestHappyPath:
    def test_minimal_one_issue(self):
        client = _client_returning({"issues": [_issue_dict("BE-001")]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_service(
            architecture=_arch(),
            enriched_spec=_spec(),
            service=_service(),
        )
        assert len(issues) == 1
        assert isinstance(issues[0], Issue)
        assert issues[0].id == "BE-001"
        assert issues[0].service == "backend"
        assert issues[0].language == "python"

    def test_topo_order_applied(self):
        # Input declares C, A, B with B → A and C → B. Output: A, B, C.
        client = _client_returning({"issues": [
            _issue_dict("C", deps=["B"]),
            _issue_dict("A"),
            _issue_dict("B", deps=["A"]),
        ]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
        )
        assert [i.id for i in issues] == ["A", "B", "C"]

    def test_service_and_language_stamped(self):
        # Even if the LLM omits service/language, we stamp them.
        client = _client_returning({"issues": [
            _issue_dict("BE-001"),
        ]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
        )
        assert issues[0].service == "backend"
        assert issues[0].language == "python"


# ── Validation ─────────────────────────────────────────────────────────


class TestValidation:
    def test_empty_issues_raises(self):
        client = _client_returning({"issues": []})
        planner = ServicePlanner(client=client)
        with pytest.raises(ServicePlannerError, match="0 issues"):
            planner.plan_service(
                architecture=_arch(), enriched_spec=_spec(), service=_service(),
            )

    def test_duplicate_id_raises(self):
        client = _client_returning({"issues": [
            _issue_dict("BE-001"),
            _issue_dict("BE-001"),
        ]})
        planner = ServicePlanner(client=client)
        with pytest.raises(ServicePlannerError, match="duplicate"):
            planner.plan_service(
                architecture=_arch(), enriched_spec=_spec(), service=_service(),
            )

    def test_unknown_dep_raises(self):
        client = _client_returning({"issues": [
            _issue_dict("BE-001", deps=["BE-999"]),
        ]})
        planner = ServicePlanner(client=client)
        with pytest.raises(ServicePlannerError, match="unknown issue id"):
            planner.plan_service(
                architecture=_arch(), enriched_spec=_spec(), service=_service(),
            )

    def test_missing_target_files_dropped(self):
        # No target_files = nothing to code. Drop the issue with a
        # warning rather than crash — repair iterations occasionally
        # surface empty-target_files issues, and losing one fix is
        # better than aborting the whole milestone.
        bad = _issue_dict("BE-001", files=[])
        good = _issue_dict("BE-002")
        client = _client_returning({"issues": [bad, good]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
        )
        assert [i.id for i in issues] == ["BE-002"]

    def test_missing_test_files_auto_filled(self):
        # No test_files = auto-fill with tests/test_<id>.py instead
        # of raising. Lets the dispatcher proceed even when the LLM
        # forgets test_files for one issue out of many.
        d = _issue_dict("BE-001", tests=[])
        client = _client_returning({"issues": [d]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
        )
        assert len(issues) == 1
        assert issues[0].test_files == ["tests/test_be_001.py"]

    def test_cyclic_deps_raises(self):
        client = _client_returning({"issues": [
            _issue_dict("A", deps=["B"]),
            _issue_dict("B", deps=["A"]),
        ]})
        planner = ServicePlanner(client=client)
        with pytest.raises(ServicePlannerError, match="cycle"):
            planner.plan_service(
                architecture=_arch(), enriched_spec=_spec(), service=_service(),
            )


# ── Prompt content ─────────────────────────────────────────────────────


class TestPromptContent:
    def test_prompt_includes_service_and_capability(self):
        client = _client_returning({"issues": [_issue_dict("BE-001")]})
        planner = ServicePlanner(client=client)
        planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user_msg = next(m.content for m in sent if m.role == "user")
        assert "backend" in user_msg.lower()
        assert "create_pet" in user_msg
        assert "Create pet" in user_msg
        assert "name" in user_msg

    def test_prompt_includes_skeleton_when_provided(self):
        client = _client_returning({"issues": [_issue_dict("BE-001")]})
        planner = ServicePlanner(client=client)
        planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
            skeleton_md="## Extension points\n- app/api/routes/",
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user_msg = next(m.content for m in sent if m.role == "user")
        assert "skeleton" in user_msg.lower()
        assert "app/api/routes/" in user_msg

    def test_prompt_includes_auth_when_provided(self):
        client = _client_returning({"issues": [_issue_dict("BE-001")]})
        planner = ServicePlanner(client=client)
        planner.plan_service(
            architecture=_arch(), enriched_spec=_spec(), service=_service(),
            auth_contract="JWT with rs256...",
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user_msg = next(m.content for m in sent if m.role == "user")
        assert "JWT" in user_msg or "auth" in user_msg.lower()


# ── Repair mode ────────────────────────────────────────────────────────


def _coverage(missing_capability="cap_x", scenario_priority="critical"):
    return CoverageReport(
        milestone_name="M1",
        approved=False,
        coverage_by_capability={"cap_x": "missing"},
        missing_scenarios=[MissingScenario(
            capability_id=missing_capability,
            scenario="duplicate email returns 409",
            priority=scenario_priority,
        )],
        recommendations=["Add duplicate-email test"],
    )


def _code_review(flagged_file="backend/app/users.py"):
    return CodeReviewReport(
        milestone_name="M1",
        approved=False,
        flagged_symbols=[FlaggedSymbol(
            file=flagged_file, line=12, symbol="get_current_user_with_roles",
            kind="import", reason="symbol not in app.core.auth",
            severity="critical",
        )],
    )


def _prior_issue(id_, files=None, refs=None):
    return Issue(
        id=id_, title=f"Implement {id_}", description="",
        service="backend", language="python",
        target_files=files or ["app/users.py"],
        test_files=[f"tests/test_{id_.lower()}.py"],
        success_criteria=[], spec_refs=refs or ["cap_x"],
        depends_on=[],
    )


class TestRepair:
    def test_repair_returns_fix_issues(self):
        client = _client_returning({"issues": [
            _issue_dict("BE-001-fix1", refs=["cap_x"]),
        ]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_repair(
            architecture=_arch(), enriched_spec=_spec(),
            service=_service(),
            prior_issues=[_prior_issue("BE-001")],
            prior_dispositions={"BE-001": "passed"},
            coverage_report=_coverage(),
            code_review_report=_code_review(),
            repair_iteration=1,
        )
        assert len(issues) == 1
        assert issues[0].id == "BE-001-fix1"
        assert issues[0].service == "backend"

    def test_repair_empty_plan_is_legal(self):
        # ServicePlanner can decide this service has no findings.
        client = _client_returning({"issues": []})
        planner = ServicePlanner(client=client)
        issues = planner.plan_repair(
            architecture=_arch(), enriched_spec=_spec(),
            service=_service(),
            prior_issues=[_prior_issue("BE-001")],
            prior_dispositions={"BE-001": "passed"},
            coverage_report=None,
            code_review_report=_code_review(flagged_file="frontend/src/x.tsx"),
            repair_iteration=1,
        )
        assert issues == []

    def test_repair_prompt_includes_findings(self):
        client = _client_returning({"issues": [_issue_dict("BE-001-fix1")]})
        planner = ServicePlanner(client=client)
        planner.plan_repair(
            architecture=_arch(), enriched_spec=_spec(),
            service=_service(),
            prior_issues=[_prior_issue("BE-001")],
            prior_dispositions={"BE-001": "passed"},
            coverage_report=_coverage(),
            code_review_report=_code_review(),
            repair_iteration=1,
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user_msg = next(m.content for m in sent if m.role == "user")
        # Findings present in the prompt
        assert "cap_x" in user_msg
        assert "duplicate email" in user_msg
        assert "get_current_user_with_roles" in user_msg
        assert "BE-001" in user_msg
        # Iteration number present
        assert "iter" in user_msg.lower()

    def test_repair_drops_unknown_dep_instead_of_raising(self):
        # Live-surfaced on CRM v1 M5 repair iter 1: LLM emitted
        # BA-fix1-3 with depends_on=['BA-fix1-2'] but never emitted
        # BA-fix1-2 itself. Greenfield raises, repair drops the edge.
        client = _client_returning({"issues": [
            _issue_dict("BA-fix1-1"),
            _issue_dict("BA-fix1-3", deps=["BA-fix1-2"]),  # bad dep
        ]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_repair(
            architecture=_arch(), enriched_spec=_spec(),
            service=_service(),
            prior_issues=[_prior_issue("BE-001")],
            prior_dispositions={"BE-001": "passed"},
            coverage_report=_coverage(),
            code_review_report=_code_review(),
            repair_iteration=1,
        )
        assert {i.id for i in issues} == {"BA-fix1-1", "BA-fix1-3"}
        # The bad dep was dropped, not preserved.
        by_id = {i.id: i for i in issues}
        assert by_id["BA-fix1-3"].depends_on == []

    def test_repair_keeps_valid_dep_alongside_dropped_bad_one(self):
        # Mix of valid + invalid deps on the same issue — keep valid,
        # drop invalid.
        client = _client_returning({"issues": [
            _issue_dict("BA-fix1-1"),
            _issue_dict("BA-fix1-2", deps=["BA-fix1-1", "BA-ghost"]),
        ]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_repair(
            architecture=_arch(), enriched_spec=_spec(),
            service=_service(),
            prior_issues=[_prior_issue("BE-001")],
            prior_dispositions={"BE-001": "passed"},
            coverage_report=_coverage(),
            code_review_report=_code_review(),
            repair_iteration=1,
        )
        by_id = {i.id: i for i in issues}
        assert by_id["BA-fix1-2"].depends_on == ["BA-fix1-1"]

    def test_repair_drops_invalid_payload_keeps_valid_siblings(self):
        # LLM emits one valid issue + one malformed issue (missing
        # required `title`). Lenient repair drops the bad one, keeps
        # the good one, milestone proceeds.
        bad = {
            "id": "BA-fix1-bad",
            # missing title
            "description": "incomplete payload",
            "target_files": ["app/x.py"],
            "test_files": ["tests/test_x.py"],
            "success_criteria": ["compiles"],
            "spec_refs": ["cap_x"],
            "depends_on": [],
        }
        good = _issue_dict("BA-fix1-good")
        client = _client_returning({"issues": [bad, good]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_repair(
            architecture=_arch(), enriched_spec=_spec(),
            service=_service(),
            prior_issues=[_prior_issue("BE-001")],
            prior_dispositions={"BE-001": "passed"},
            coverage_report=_coverage(),
            code_review_report=_code_review(),
            repair_iteration=1,
        )
        assert [i.id for i in issues] == ["BA-fix1-good"]

    def test_repair_all_payloads_invalid_returns_empty(self):
        # Every payload is bad — return [] rather than raise. Milestone
        # proceeds to next gate (which can then re-trigger or halt
        # based on findings).
        bad_a = {"id": "X", "description": "no title"}
        bad_b = {"id": "Y", "description": "no title"}
        client = _client_returning({"issues": [bad_a, bad_b]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_repair(
            architecture=_arch(), enriched_spec=_spec(),
            service=_service(),
            prior_issues=[_prior_issue("BE-001")],
            prior_dispositions={"BE-001": "passed"},
            coverage_report=_coverage(),
            code_review_report=_code_review(),
            repair_iteration=1,
        )
        assert issues == []

    def test_repair_breaks_cycle_instead_of_raising(self):
        # A → B → A cycle. Lenient repair drops the inter-cycle edges
        # and re-topo-sorts. Both issues should survive with empty
        # depends_on.
        client = _client_returning({"issues": [
            _issue_dict("A", deps=["B"]),
            _issue_dict("B", deps=["A"]),
        ]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_repair(
            architecture=_arch(), enriched_spec=_spec(),
            service=_service(),
            prior_issues=[_prior_issue("BE-001")],
            prior_dispositions={"BE-001": "passed"},
            coverage_report=_coverage(),
            code_review_report=_code_review(),
            repair_iteration=1,
        )
        assert {i.id for i in issues} == {"A", "B"}
        by_id = {i.id: i for i in issues}
        # Both edges (both endpoints in cycle) dropped.
        assert by_id["A"].depends_on == []
        assert by_id["B"].depends_on == []

    def test_repair_cycle_preserves_deps_outside_cycle_set(self):
        # D depends on E (both outside the A↔B cycle). Even when A↔B
        # are reported as cyclic, D's dep on E must survive.
        # Note: ``cyclic_ids`` from Kahn's algorithm includes both
        # items strictly IN the cycle AND items merely blocked behind
        # it. So a third issue C that depends on A WILL also lose its
        # edge — that's the cost of best-effort repair: blocked-behind
        # items lose ordering hint but the milestone keeps moving.
        client = _client_returning({"issues": [
            _issue_dict("A", deps=["B"]),
            _issue_dict("B", deps=["A"]),
            _issue_dict("D"),
            _issue_dict("E", deps=["D"]),
        ]})
        planner = ServicePlanner(client=client)
        issues = planner.plan_repair(
            architecture=_arch(), enriched_spec=_spec(),
            service=_service(),
            prior_issues=[_prior_issue("BE-001")],
            prior_dispositions={"BE-001": "passed"},
            coverage_report=_coverage(),
            code_review_report=_code_review(),
            repair_iteration=1,
        )
        by_id = {i.id: i for i in issues}
        # Cycle edges removed (A and B are in the cycle).
        assert by_id["A"].depends_on == []
        assert by_id["B"].depends_on == []
        # D and E are entirely outside the cycle reachability — their
        # edge survives.
        assert by_id["D"].depends_on == []
        assert by_id["E"].depends_on == ["D"]

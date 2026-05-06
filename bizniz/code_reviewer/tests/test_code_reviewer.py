"""Tests for CodeReviewer."""
import json
from unittest.mock import MagicMock

import pytest

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.code_reviewer.agent import CodeReviewer
from bizniz.code_reviewer.types import (
    AntiPatternViolation,
    CodeReviewError,
    CodeReviewReport,
    FlaggedSymbol,
    MissingErrorHandling,
    UngatedAuthCapability,
)
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.types import (
    CapabilitySpec,
    EnrichedSpec,
    Field as SpecField,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _milestone():
    return Milestone(
        sequence_index=1, name="Pet CRUD",
        problem_slice="CRUD pets.", use_cases=[],
        success_criteria=[],
    )


def _spec():
    return EnrichedSpec(
        milestone_name="Pet CRUD",
        capabilities=[
            CapabilitySpec(
                id="create_pet", name="Create Pet", description="d",
                inputs=[], outputs=[], validation_rules=[],
                error_cases=["duplicate name → 409"],
                edge_cases=[],
                auth_required=True, allowed_roles=["groomer"],
                test_scenarios=[],
            ),
        ],
        anti_patterns=["never log raw passwords"],
    )


def _payload(
    *,
    approved=True,
    flagged=None,
    anti=None,
    ungated=None,
    missing=None,
    summary="ok",
    confidence=0.9,
):
    return {
        "milestone_name": "Pet CRUD",
        "approved": approved,
        "flagged_symbols": flagged or [],
        "anti_pattern_violations": anti or [],
        "ungated_auth": ungated or [],
        "missing_error_handling": missing or [],
        "recommendations": [],
        "summary": summary,
        "confidence": confidence,
    }


def _client_returning(payload):
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = (json.dumps(payload), "job", [])
    return client


# ── Behavior ───────────────────────────────────────────────────────────


class TestReview:
    def test_returns_report(self):
        client = _client_returning(_payload())
        cr = CodeReviewer(client=client)
        result = cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"app/api/pets.py": "x = 1"},
        )
        assert isinstance(result, CodeReviewReport)
        assert result.approved is True
        assert result.milestone_name == "Pet CRUD"

    def test_no_changed_files_returns_empty_pass(self):
        client = MagicMock(spec=BaseAIClient)
        cr = CodeReviewer(client=client)
        result = cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={},
        )
        assert result.approved is True
        assert result.confidence == 0.0
        # LLM not called — fast path.
        client.get_text.assert_not_called()

    def test_canonicalizes_milestone_name(self):
        payload = _payload()
        payload["milestone_name"] = "wrong"
        client = _client_returning(payload)
        cr = CodeReviewer(client=client)
        result = cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.py": "y"},
        )
        assert result.milestone_name == "Pet CRUD"

    def test_uses_json_schema_response_format(self):
        client = _client_returning(_payload())
        cr = CodeReviewer(client=client)
        cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.py": "y"},
        )
        kwargs = client.get_text.call_args.kwargs
        assert kwargs["schema"]["name"] == "CodeReviewReport"


# ── Approval override ─────────────────────────────────────────────────


class TestApprovalOverride:
    def test_critical_flagged_symbol_overrides_approval(self):
        payload = _payload(
            approved=True,
            flagged=[{
                "file": "app/api/pets.py", "line": 12,
                "symbol": "UnknownThing", "kind": "import",
                "reason": "fabricated", "severity": "critical",
            }],
        )
        client = _client_returning(payload)
        cr = CodeReviewer(client=client)
        result = cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.py": "y"},
        )
        assert result.approved is False

    def test_warning_only_does_not_override_approval(self):
        payload = _payload(
            approved=True,
            flagged=[{
                "file": "x.py", "line": 1,
                "symbol": "stuff", "kind": "type",
                "reason": "looks weird", "severity": "warning",
            }],
        )
        client = _client_returning(payload)
        cr = CodeReviewer(client=client)
        result = cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.py": "y"},
        )
        assert result.approved is True

    def test_critical_anti_pattern_overrides_approval(self):
        payload = _payload(
            approved=True,
            anti=[{
                "file": "x.py", "line": 5,
                "anti_pattern": "never log raw passwords",
                "evidence": "logger.info(password)",
                "severity": "critical",
            }],
        )
        cr = CodeReviewer(client=_client_returning(payload))
        result = cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.py": "y"},
        )
        assert result.approved is False

    def test_critical_ungated_auth_overrides_approval(self):
        payload = _payload(
            approved=True,
            ungated=[{
                "file": "app/routes/pets.py",
                "capability_id": "create_pet",
                "evidence": "POST /pets has no auth dep",
                "severity": "critical",
            }],
        )
        cr = CodeReviewer(client=_client_returning(payload))
        result = cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.py": "y"},
        )
        assert result.approved is False


# ── Schema-failure path ────────────────────────────────────────────────


class TestBadResponse:
    def test_invalid_schema_raises(self):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = (
            json.dumps({"approved": "yes", "confidence": 12}), "j", [],
        )
        cr = CodeReviewer(client=client)
        with pytest.raises(CodeReviewError, match="schema validation"):
            cr.review(
                milestone=_milestone(), enriched_spec=_spec(),
                changed_files={"x.py": "y"},
            )


# ── Prompt threading ───────────────────────────────────────────────────


class TestPromptThreading:
    def test_changed_files_with_line_numbers_in_prompt(self):
        client = _client_returning(_payload())
        cr = CodeReviewer(client=client)
        cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"app/x.py": "import foo\nbar = 1"},
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user = next(m.content for m in sent if m.role == "user")
        assert "app/x.py" in user
        assert "import foo" in user
        # Line numbers prefixed
        assert "   1  import foo" in user
        assert "   2  bar = 1" in user

    def test_existing_symbols_threaded(self):
        client = _client_returning(_payload())
        cr = CodeReviewer(client=client)
        cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.py": "y"},
            existing_symbols="fastapi.APIRouter\nhttpx.AsyncClient",
        )
        user = next(
            m.content for m in client.get_text.call_args.kwargs["messages"]
            if m.role == "user"
        )
        assert "Existing symbols" in user
        assert "fastapi.APIRouter" in user

    def test_auth_contract_threaded(self):
        client = _client_returning(_payload())
        cr = CodeReviewer(client=client)
        cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.py": "y"},
            auth_contract="role: groomer",
        )
        user = next(
            m.content for m in client.get_text.call_args.kwargs["messages"]
            if m.role == "user"
        )
        assert "Auth contract" in user
        assert "groomer" in user

    def test_framework_calibration_threaded_when_architecture_provided(self):
        from bizniz.architect.types import ServiceDefinition, SystemArchitecture
        arch = SystemArchitecture(
            project_name="P", project_slug="p", description="d",
            services=[
                ServiceDefinition(
                    name="frontend", service_type="frontend",
                    framework="angular", language="typescript",
                    description="UI", workspace_name="frontend", port=4200,
                ),
            ],
        )
        client = _client_returning(_payload())
        cr = CodeReviewer(client=client)
        cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.ts": "y"},
            architecture=arch,
        )
        user = next(
            m.content for m in client.get_text.call_args.kwargs["messages"]
            if m.role == "user"
        )
        assert "Framework calibration" in user
        assert "angular" in user.lower()
        assert "@Component" in user
        assert "@NgModule" in user

    def test_no_framework_calibration_when_architecture_omitted(self):
        client = _client_returning(_payload())
        cr = CodeReviewer(client=client)
        cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.py": "y"},
        )
        user = next(
            m.content for m in client.get_text.call_args.kwargs["messages"]
            if m.role == "user"
        )
        assert "Framework calibration" not in user

    def test_prior_specs_threaded(self):
        client = _client_returning(_payload())
        cr = CodeReviewer(client=client)
        prior = EnrichedSpec(
            milestone_name="M0",
            capabilities=[CapabilitySpec(
                id="prior_cap", name="N", description="d",
                inputs=[], outputs=[], validation_rules=[],
                error_cases=[], edge_cases=[],
                auth_required=True, allowed_roles=[], test_scenarios=[],
            )],
        )
        cr.review(
            milestone=_milestone(), enriched_spec=_spec(),
            changed_files={"x.py": "y"},
            prior_specs=[prior],
        )
        user = next(
            m.content for m in client.get_text.call_args.kwargs["messages"]
            if m.role == "user"
        )
        assert "prior_cap" in user


# ── Report convenience ─────────────────────────────────────────────────


class TestReportProperties:
    def test_critical_findings_aggregates_categories(self):
        report = CodeReviewReport(
            milestone_name="m", approved=False,
            flagged_symbols=[FlaggedSymbol(
                file="a", symbol="X", kind="import",
                reason="fake", severity="critical",
            ), FlaggedSymbol(
                file="b", symbol="Y", kind="type",
                reason="iffy", severity="warning",
            )],
            anti_pattern_violations=[AntiPatternViolation(
                file="c", anti_pattern="rule", evidence="e",
                severity="critical",
            )],
            ungated_auth=[UngatedAuthCapability(
                file="d", capability_id="cap", evidence="e",
                severity="critical",
            )],
        )
        assert report.total_findings == 4
        assert len(report.critical_findings) == 3
        assert report.has_critical is True

    def test_no_critical_means_clean(self):
        report = CodeReviewReport(
            milestone_name="m", approved=True,
            flagged_symbols=[FlaggedSymbol(
                file="a", symbol="X", kind="type",
                reason="iffy", severity="warning",
            )],
        )
        assert report.has_critical is False

    def test_render_for_repair(self):
        report = CodeReviewReport(
            milestone_name="Pet CRUD", approved=False,
            flagged_symbols=[FlaggedSymbol(
                file="app/x.py", line=12, symbol="ghost",
                kind="import", reason="not in fastapi",
                severity="critical",
            )],
            recommendations=["use APIRouter from fastapi"],
            summary="One fabricated import.",
        )
        out = report.render_for_repair()
        assert "Pet CRUD" in out
        assert "CHANGES REQUESTED" in out
        assert "ghost" in out
        assert "app/x.py:12" in out
        assert "use APIRouter" in out

    def test_render_includes_anti_patterns(self):
        report = CodeReviewReport(
            milestone_name="m", approved=False,
            anti_pattern_violations=[AntiPatternViolation(
                file="x.py", line=5,
                anti_pattern="never log passwords",
                evidence="logger.info(password)",
                severity="critical",
            )],
        )
        out = report.render_for_repair()
        assert "never log passwords" in out
        assert "x.py:5" in out

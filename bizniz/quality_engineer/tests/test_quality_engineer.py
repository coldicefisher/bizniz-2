"""Tests for QualityEngineer (enrich + review).

Mock the LLM client. Verify the agent constructs prompts correctly,
validates output, enforces invariants (no empty capabilities, no
duplicate ids, source-files firewall on review), and post-processes
the report (forced approval=false on gaps, fill-in-missing).
"""
import inspect
import json
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.planner.types import Milestone
from bizniz.quality_engineer.agent import QualityEngineer, _summarize_architecture
from bizniz.quality_engineer.types import (
    CapabilitySpec,
    CoverageReport,
    EnrichedSpec,
    Field,
    QualityEngineerError,
)


# ── Fixtures ───────────────────────────────────────────────────────────


def _arch():
    return SystemArchitecture(
        project_name="Pet Groomer",
        project_slug="pet_groomer",
        description="Booking + roster",
        services=[
            ServiceDefinition(
                name="auth", service_type="auth", framework="fusionauth",
                language="yaml", description="Identity provider.",
                workspace_name="fusionauth", port=9011,
            ),
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="REST API.",
                workspace_name="backend", port=8000, depends_on=["auth"],
            ),
            ServiceDefinition(
                name="frontend", service_type="frontend", framework="react",
                language="typescript", description="UI.",
                workspace_name="frontend", port=5173, depends_on=["backend"],
            ),
        ],
    )


def _milestone():
    return Milestone(
        sequence_index=1,
        name="Pet CRUD",
        problem_slice="Allow groomers to create/list/edit/delete pet records.",
        use_cases=["Add a pet", "Update pet weight"],
        success_criteria=["pet visible in list after create"],
        depends_on_names=[],
        estimated_effort="medium",
    )


def _enriched_spec_payload(caps: int = 2, confidence: float = 0.9) -> dict:
    return {
        "milestone_name": "Pet CRUD",
        "capabilities": [
            {
                "id": f"cap_{i}",
                "name": f"Capability {i}",
                "description": "desc",
                "inputs": [
                    {"name": "x", "type": "string", "required": True,
                     "constraints": [], "description": ""}
                ],
                "outputs": [],
                "validation_rules": [],
                "error_cases": [],
                "edge_cases": [],
                "auth_required": True,
                "allowed_roles": ["groomer"],
                "test_scenarios": ["happy path"],
            }
            for i in range(caps)
        ],
        "cross_cutting": {"error_handling": ["envelope shape"]},
        "anti_patterns": ["no plaintext passwords"],
        "confidence": confidence,
    }


def _coverage_payload(verdicts=None, approved=True, missing_scenarios=None):
    return {
        "milestone_name": "Pet CRUD",
        "approved": approved,
        "coverage_by_capability": verdicts or {"cap_0": "covered", "cap_1": "covered"},
        "missing_scenarios": missing_scenarios or [],
        "recommendations": [],
        "bias_check_passed": True,
        "summary": "ok",
        "confidence": 0.9,
    }


def _client_returning(payload):
    """Return a mock BaseAIClient whose get_text returns ``payload`` (dict)
    serialized as JSON."""
    client = MagicMock(spec=BaseAIClient)
    client.get_text.return_value = (json.dumps(payload), "job-id", [])
    return client


# ── enrich ─────────────────────────────────────────────────────────────


class TestEnrich:
    def test_returns_enriched_spec(self):
        client = _client_returning(_enriched_spec_payload(caps=2))
        qe = QualityEngineer(client=client)
        result = qe.enrich(milestone=_milestone(), architecture=_arch())
        assert isinstance(result, EnrichedSpec)
        assert result.milestone_name == "Pet CRUD"
        assert len(result.capabilities) == 2
        assert result.capabilities[0].id == "cap_0"

    def test_milestone_name_is_canonicalized(self):
        # Even if LLM returns wrong name, agent forces it.
        payload = _enriched_spec_payload()
        payload["milestone_name"] = "Wrong name"
        client = _client_returning(payload)
        qe = QualityEngineer(client=client)
        result = qe.enrich(milestone=_milestone(), architecture=_arch())
        assert result.milestone_name == "Pet CRUD"

    def test_rejects_empty_capabilities(self):
        # Schema requires minItems 1, but mock bypasses schema. Test the
        # agent's own guard.
        payload = _enriched_spec_payload(caps=0)
        # Need at least 1 to pass model_validate on min items, but our
        # Pydantic doesn't enforce that — so this falls into the empty
        # check in agent.py.
        client = _client_returning(payload)
        qe = QualityEngineer(client=client)
        with pytest.raises(QualityEngineerError, match="zero capabilities"):
            qe.enrich(milestone=_milestone(), architecture=_arch())

    def test_rejects_duplicate_capability_ids(self):
        payload = _enriched_spec_payload(caps=2)
        payload["capabilities"][1]["id"] = "cap_0"  # dup
        client = _client_returning(payload)
        qe = QualityEngineer(client=client)
        with pytest.raises(QualityEngineerError, match="duplicate capability ids"):
            qe.enrich(milestone=_milestone(), architecture=_arch())

    def test_threads_architecture_into_prompt(self):
        client = _client_returning(_enriched_spec_payload())
        qe = QualityEngineer(client=client)
        qe.enrich(milestone=_milestone(), architecture=_arch())
        sent = client.get_text.call_args.kwargs["messages"]
        user = next(m.content for m in sent if m.role == "user")
        assert "Pet Groomer" in user
        assert "fastapi" in user
        assert "react" in user

    def test_threads_auth_contract(self):
        client = _client_returning(_enriched_spec_payload())
        qe = QualityEngineer(client=client)
        qe.enrich(
            milestone=_milestone(), architecture=_arch(),
            auth_contract="# Auth\nrole: groomer",
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user = next(m.content for m in sent if m.role == "user")
        assert "Auth contract" in user
        assert "groomer" in user

    def test_threads_prior_specs(self):
        client = _client_returning(_enriched_spec_payload())
        qe = QualityEngineer(client=client)
        prior = EnrichedSpec(
            milestone_name="M0", capabilities=[
                CapabilitySpec(id="prior_cap", name="N", description="d",
                               inputs=[], outputs=[], validation_rules=[],
                               error_cases=[], edge_cases=[],
                               auth_required=True, allowed_roles=[],
                               test_scenarios=[]),
            ],
        )
        qe.enrich(
            milestone=_milestone(), architecture=_arch(),
            prior_specs=[prior],
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user = next(m.content for m in sent if m.role == "user")
        assert "prior_cap" in user

    def test_uses_json_schema_response_format(self):
        client = _client_returning(_enriched_spec_payload())
        qe = QualityEngineer(client=client)
        qe.enrich(milestone=_milestone(), architecture=_arch())
        kwargs = client.get_text.call_args.kwargs
        assert kwargs.get("schema") is not None
        assert kwargs["schema"]["name"] == "EnrichedSpec"


# ── review ─────────────────────────────────────────────────────────────


class TestReview:
    def _spec(self):
        return EnrichedSpec(
            milestone_name="Pet CRUD",
            capabilities=[
                CapabilitySpec(
                    id="cap_0", name="N0", description="d",
                    inputs=[], outputs=[], validation_rules=[],
                    error_cases=[], edge_cases=[],
                    auth_required=True, allowed_roles=[], test_scenarios=[],
                ),
                CapabilitySpec(
                    id="cap_1", name="N1", description="d",
                    inputs=[], outputs=[], validation_rules=[],
                    error_cases=[], edge_cases=[],
                    auth_required=True, allowed_roles=[], test_scenarios=[],
                ),
            ],
        )

    def test_returns_coverage_report(self):
        client = _client_returning(_coverage_payload())
        qe = QualityEngineer(client=client)
        result = qe.review(
            milestone=_milestone(),
            enriched_spec=self._spec(),
            engineer_plan={"issues": []},
            test_files={"tests/x.py": "def test_x(): assert True"},
        )
        assert isinstance(result, CoverageReport)
        assert result.approved is True

    def test_review_signature_has_no_source_files_param(self):
        """Bias firewall is enforced via the call shape."""
        sig = inspect.signature(QualityEngineer.review)
        assert "source_files" not in sig.parameters
        assert "source" not in sig.parameters
        assert "implementation" not in sig.parameters

    def test_fills_missing_capabilities_as_missing(self):
        # LLM only rated cap_0; agent must fill cap_1 as "missing".
        payload = _coverage_payload(
            verdicts={"cap_0": "covered"},
            approved=True,
        )
        client = _client_returning(payload)
        qe = QualityEngineer(client=client)
        result = qe.review(
            milestone=_milestone(),
            enriched_spec=self._spec(),
            engineer_plan={"issues": []},
            test_files={},
        )
        assert result.coverage_by_capability["cap_0"] == "covered"
        assert result.coverage_by_capability["cap_1"] == "missing"
        # And approval should be forced false because a cap is missing.
        assert result.approved is False

    def test_drops_unknown_capability_ids(self):
        payload = _coverage_payload(
            verdicts={
                "cap_0": "covered", "cap_1": "covered", "ghost": "covered",
            },
        )
        client = _client_returning(payload)
        qe = QualityEngineer(client=client)
        result = qe.review(
            milestone=_milestone(),
            enriched_spec=self._spec(),
            engineer_plan={"issues": []},
            test_files={},
        )
        assert "ghost" not in result.coverage_by_capability
        assert result.coverage_by_capability["cap_0"] == "covered"

    def test_forces_unapproved_when_critical_gap(self):
        payload = _coverage_payload(
            verdicts={"cap_0": "covered", "cap_1": "covered"},
            approved=True,
            missing_scenarios=[{
                "capability_id": "cap_0",
                "scenario": "auth bypass",
                "priority": "critical",
            }],
        )
        client = _client_returning(payload)
        qe = QualityEngineer(client=client)
        result = qe.review(
            milestone=_milestone(),
            enriched_spec=self._spec(),
            engineer_plan={"issues": []},
            test_files={},
        )
        assert result.approved is False

    def test_keeps_approved_when_all_covered_no_critical(self):
        payload = _coverage_payload(
            verdicts={"cap_0": "covered", "cap_1": "covered"},
            approved=True,
            missing_scenarios=[{
                "capability_id": "cap_1",
                "scenario": "Unicode edge case",
                "priority": "nice-to-have",
            }],
        )
        client = _client_returning(payload)
        qe = QualityEngineer(client=client)
        result = qe.review(
            milestone=_milestone(),
            enriched_spec=self._spec(),
            engineer_plan={"issues": []},
            test_files={},
        )
        assert result.approved is True

    def test_test_files_threaded_into_prompt(self):
        client = _client_returning(_coverage_payload())
        qe = QualityEngineer(client=client)
        qe.review(
            milestone=_milestone(),
            enriched_spec=self._spec(),
            engineer_plan={"issues": [{"id": "I1", "spec_refs": ["cap_0"]}]},
            test_files={"tests/test_a.py": "ASSERT_THIS"},
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user = next(m.content for m in sent if m.role == "user")
        assert "tests/test_a.py" in user
        assert "ASSERT_THIS" in user
        assert "spec_refs" in user

    def test_no_test_files_warns_in_prompt(self):
        client = _client_returning(_coverage_payload())
        qe = QualityEngineer(client=client)
        qe.review(
            milestone=_milestone(),
            enriched_spec=self._spec(),
            engineer_plan={"issues": []},
            test_files={},
        )
        sent = client.get_text.call_args.kwargs["messages"]
        user = next(m.content for m in sent if m.role == "user")
        assert "no test files" in user.lower()

    def test_bad_response_returns_lenient_fallback(self):
        # Lenient repair-mode counterpart to the prior "raises" test.
        # Review is side-channel — bad JSON shouldn't halt the milestone.
        # Wrong type for ``approved`` -> schema validation fails.
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = (
            json.dumps({"approved": "yes please", "confidence": 2.0}),
            "id", [],
        )
        qe = QualityEngineer(client=client)
        report = qe.review(
            milestone=_milestone(),
            enriched_spec=self._spec(),
            engineer_plan={},
            test_files={},
        )
        # Conservative: not approved, zero confidence, fallback summary.
        assert report.approved is False
        assert report.confidence == 0.0
        assert "auto-fallback" in report.summary
        assert any("malformed JSON" in r for r in report.recommendations)
        # No findings — repair iter will re-trigger but won't loop on
        # phantom issues. Max-repair-iter cap will eventually accept.
        assert report.coverage_by_capability == {}
        assert report.missing_scenarios == []


# ── re_enrich ──────────────────────────────────────────────────────────


class TestReEnrich:
    """Re-enrich is a side-channel (called only when confidence is
    0.4-0.6). Failures should fall back to the prior spec rather
    than halt — the original is still usable, just less confident."""

    def _prior(self, confidence: float = 0.5) -> EnrichedSpec:
        return EnrichedSpec(
            milestone_name="Pet CRUD",
            capabilities=[
                CapabilitySpec(
                    id="cap_0", name="Create pet",
                    description="Create a pet record",
                    inputs=[Field(name="name", type="string", required=True)],
                    outputs=[], validation_rules=[], error_cases=[],
                    edge_cases=[], auth_required=True,
                    allowed_roles=["groomer"],
                    test_scenarios=["happy path"],
                ),
            ],
            confidence=confidence,
        )

    def test_success_returns_new_spec(self):
        # Sanity baseline.
        client = _client_returning(_enriched_spec_payload(caps=2, confidence=0.85))
        qe = QualityEngineer(client=client)
        spec = qe.re_enrich(
            milestone=_milestone(),
            prior_spec=self._prior(confidence=0.5),
            architecture=_arch(),
        )
        assert spec.confidence == 0.85
        assert len(spec.capabilities) == 2

    def test_bad_json_falls_back_to_prior_spec(self):
        # Schema validation fails → return prior_spec, do not raise.
        prior = self._prior(confidence=0.5)
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = (
            json.dumps({"capabilities": "not a list"}),
            "id", [],
        )
        qe = QualityEngineer(client=client)
        result = qe.re_enrich(
            milestone=_milestone(),
            prior_spec=prior,
            architecture=_arch(),
        )
        # Returned the prior spec unchanged.
        assert result is prior
        assert result.confidence == 0.5

    def test_empty_capabilities_falls_back_to_prior_spec(self):
        # LLM returns valid JSON but zero capabilities — also a
        # fallback case since a milestone without capabilities is
        # meaningless.
        prior = self._prior(confidence=0.5)
        client = _client_returning(_enriched_spec_payload(caps=0, confidence=0.9))
        qe = QualityEngineer(client=client)
        result = qe.re_enrich(
            milestone=_milestone(),
            prior_spec=prior,
            architecture=_arch(),
        )
        assert result is prior
        assert result.confidence == 0.5


# ── helpers ────────────────────────────────────────────────────────────


class TestSummarizeArchitecture:
    def test_lists_all_services(self):
        s = _summarize_architecture(_arch())
        assert "Pet Groomer" in s
        assert "auth" in s
        assert "backend" in s
        assert "frontend" in s
        assert "fastapi" in s
        assert "react" in s

    def test_includes_dependencies(self):
        s = _summarize_architecture(_arch())
        assert "depends_on: auth" in s
        assert "depends_on: backend" in s


# ── CoverageReport convenience properties ─────────────────────────────


class TestCoverageReportProps:
    def test_covered_count(self):
        r = CoverageReport(
            milestone_name="m", approved=True,
            coverage_by_capability={"a": "covered", "b": "partial", "c": "covered"},
        )
        assert r.covered_count == 2
        assert r.total_count == 3
        assert r.coverage_ratio() == pytest.approx(2 / 3)

    def test_zero_total(self):
        r = CoverageReport(milestone_name="m", approved=True)
        assert r.coverage_ratio() == 0.0

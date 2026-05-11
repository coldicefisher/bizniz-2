"""Tests for SmokePhase.

Live HTTP probing isn't covered here — that needs a running compose
stack and is exercised end-to-end via examples/v2_build.py. These
tests cover the deterministic pieces: contract parsing, service
filtering, result aggregation.
"""
from pathlib import Path
from unittest.mock import patch, MagicMock

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.driver.smoke_phase import (
    SmokeCheck,
    SmokePhase,
    SmokePhaseResult,
)
from bizniz.planner.types import Milestone


_CONTRACT_FIXTURE = """\
# Auth Contract

## FusionAuth coordinates

- Host URL: `http://localhost:9019`
- Primary application ID: `85a03867-dccf-4882-adde-1a79aeec50df`
- Tenant ID: `d0c3cafd-c722-4ee2-994c-6df745cadc08`
- Issuer (iss claim): `acme.com`

## Test users

Format: `email / password — roles role_name`.

- landlord@example.com / password — roles landlord ✓
- tenant@example.com / Password123! — roles tenant ✓
"""


class TestContractParsing:
    def test_parses_primary_app_id(self):
        assert SmokePhase._parse_primary_app_id(_CONTRACT_FIXTURE) == (
            "85a03867-dccf-4882-adde-1a79aeec50df"
        )

    def test_returns_none_on_missing_app_id(self):
        assert SmokePhase._parse_primary_app_id("# Empty") is None

    def test_parses_test_users(self):
        users = SmokePhase._parse_test_users(_CONTRACT_FIXTURE)
        assert ("landlord@example.com", "password") in users
        assert ("tenant@example.com", "Password123!") in users
        assert len(users) == 2

    def test_skips_dashes_outside_user_section(self):
        c = (
            "# Auth\n## Roles\n- admin — desc\n## Test users\n\n"
            "- u@e.com / pw — roles x ✓\n"
        )
        users = SmokePhase._parse_test_users(c)
        assert users == [("u@e.com", "pw")]


class TestServiceSelection:
    def _arch(self, services):
        return SystemArchitecture(
            project_name="t", project_slug="t",
            services=services, description="",
        )

    def test_find_fa_service_matches_service_type(self):
        s = ServiceDefinition(
            name="auth", service_type="auth", framework="fusionauth",
            language="java", description="", workspace_name="auth",
            port=9019,
        )
        b = ServiceDefinition(
            name="backend", service_type="backend", framework="fastapi",
            language="python", description="", workspace_name="backend",
            port=8000,
        )
        arch = self._arch([b, s])
        assert SmokePhase._find_fa_service(arch) is s

    def test_no_fa_service_returns_none(self):
        b = ServiceDefinition(
            name="backend", service_type="backend", framework="fastapi",
            language="python", description="", workspace_name="backend",
            port=8000,
        )
        assert SmokePhase._find_fa_service(self._arch([b])) is None


class TestResultAggregation:
    def _milestone(self):
        return Milestone(
            name="M1",
            description="d",
            problem_slice="ps",
            sequence_index=0,
        )

    def _arch_with_backend(self):
        return SystemArchitecture(
            project_name="t", project_slug="t",
            services=[
                ServiceDefinition(
                    name="backend", service_type="backend",
                    framework="fastapi", language="python",
                    description="", workspace_name="backend", port=8000,
                ),
            ],
            description="",
        )

    def test_health_failure_is_critical(self):
        phase = SmokePhase()
        # Patch requests so the health probe gets connection error
        with patch(
            "bizniz.driver.smoke_phase.requests.get",
            side_effect=ConnectionError("refused"),
        ):
            result = phase.run(
                milestone=self._milestone(),
                architecture=self._arch_with_backend(),
                project_root=Path("/tmp"),
                auth_contract=None,
            )
        assert not result.passed
        assert any("health[backend]" in f for f in result.critical_failures)

    def test_no_backends_passes_with_no_checks(self):
        phase = SmokePhase()
        empty_arch = SystemArchitecture(
            project_name="t", project_slug="t", services=[], description="",
        )
        result = phase.run(
            milestone=self._milestone(),
            architecture=empty_arch,
            project_root=Path("/tmp"),
            auth_contract=None,
        )
        assert result.passed
        assert result.checks == []

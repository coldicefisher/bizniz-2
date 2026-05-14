"""Tests for the framework conventions catalog."""
import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.lib.framework_conventions import (
    known_frameworks,
    render_for_engineer,
    render_for_reviewer,
)


def _arch(*frameworks_and_languages):
    """Build a quick architecture from (framework, language) pairs."""
    services = []
    for i, (fw, lang) in enumerate(frameworks_and_languages):
        services.append(
            ServiceDefinition(
                name=f"svc{i}", service_type="frontend" if "react" in fw or "angular" in fw else "backend",
                framework=fw, language=lang,
                description="d", workspace_name=f"w{i}", port=3000 + i,
            )
        )
    return SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=services,
    )


class TestKnownFrameworks:
    def test_includes_react_angular_fastapi(self):
        kn = known_frameworks()
        assert "react" in kn
        assert "angular" in kn
        assert "fastapi" in kn


class TestRenderForEngineer:
    def test_react_block_emitted(self):
        out = render_for_engineer(_arch(("react", "typescript")))
        assert "Framework conventions" in out
        assert "react" in out.lower()
        assert ".tsx" in out
        assert "Vite" in out
        assert "src/routes/*.tsx" in out
        assert "Tailwind" in out

    def test_angular_block_emitted(self):
        out = render_for_engineer(_arch(("angular", "typescript")))
        assert "angular" in out.lower()
        assert ".component.ts" in out
        assert "Angular CLI" in out
        assert "Angular Material" in out
        assert "RxJS" in out

    def test_fastapi_block_emitted(self):
        out = render_for_engineer(_arch(("fastapi", "python")))
        assert "fastapi" in out.lower()
        assert "auto-mounts app/api/routes/" in out
        assert "Pydantic v2" in out
        assert "FusionAuth" in out

    def test_multi_service_emits_both(self):
        out = render_for_engineer(
            _arch(("fastapi", "python"), ("react", "typescript"))
        )
        assert "fastapi" in out.lower()
        assert "react" in out.lower()

    def test_dedupes_repeated_framework(self):
        # Two react services — only one block.
        out = render_for_engineer(
            _arch(("react", "typescript"), ("react", "typescript"))
        )
        assert out.lower().count("### react") == 1

    def test_unknown_framework_skipped(self):
        out = render_for_engineer(_arch(("svelte", "typescript")))
        # No facts for svelte; output should be empty.
        assert out == ""

    def test_mixed_known_and_unknown(self):
        out = render_for_engineer(
            _arch(("react", "typescript"), ("svelte", "typescript"))
        )
        assert "react" in out.lower()
        assert "svelte" not in out.lower()

    def test_empty_architecture_returns_empty(self):
        arch = SystemArchitecture(
            project_name="P", project_slug="p", description="", services=[],
        )
        assert render_for_engineer(arch) == ""


class TestRenderForReviewer:
    def test_react_calibration(self):
        out = render_for_reviewer(_arch(("react", "typescript")))
        assert "Framework calibration" in out
        assert "DO NOT flag" in out
        assert "src/routes" in out
        assert "RouteEntry" in out
        assert "Tailwind" in out

    def test_angular_calibration(self):
        out = render_for_reviewer(_arch(("angular", "typescript")))
        assert "@Component" in out
        assert "@Injectable" in out
        assert "@NgModule" in out
        assert "RxJS" in out
        assert "signal" in out  # Angular signals

    def test_fastapi_calibration(self):
        out = render_for_reviewer(_arch(("fastapi", "python")))
        assert "auto-mounted" in out
        assert "Mapped[X]" in out or "mapped_column" in out
        assert "Depends" in out

    def test_distinct_voice_from_engineer(self):
        # Engineer voice: imperative ("Use..."). Reviewer voice: descriptive
        # ("X is real"). Verify they emit different headers.
        eng = render_for_engineer(_arch(("react", "typescript")))
        rev = render_for_reviewer(_arch(("react", "typescript")))
        assert "use these patterns" in eng.lower()
        assert "do not flag" in rev.lower()

    def test_empty_architecture_returns_empty(self):
        arch = SystemArchitecture(
            project_name="P", project_slug="p", description="", services=[],
        )
        assert render_for_reviewer(arch) == ""

    def test_dedupes(self):
        out = render_for_reviewer(
            _arch(("angular", "typescript"), ("angular", "typescript"))
        )
        assert out.lower().count("### angular") == 1

    def test_case_insensitive_framework_name(self):
        # ServiceDefinition normalizes to lowercase, but be defensive.
        out = render_for_reviewer(_arch(("React", "TypeScript")))
        # ServiceDefinition lowercases framework, so expect react block.
        assert "react" in out.lower()

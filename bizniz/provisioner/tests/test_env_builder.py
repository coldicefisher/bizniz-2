"""Tests for the .env builder."""
from bizniz.architect.types import SystemArchitecture
from bizniz.provisioner.env_builder import build_env_file


def _arch() -> SystemArchitecture:
    return SystemArchitecture(
        project_name="Pet Groomer", project_slug="pet_groomer",
        services=[], description="t",
    )


def test_env_includes_project_name():
    out = build_env_file(_arch(), {})
    assert "PROJECT_NAME=pet_groomer" in out


def test_env_groups_by_prefix():
    template_env = {
        "POSTGRES_USER": "dev",
        "POSTGRES_DB": "pet_groomer",
        "REDIS_URL": "redis://redis:6379/0",
        "FUSIONAUTH_ADMIN_EMAIL": "admin@x.local",
        "FUSIONAUTH_API_KEY": "abc",
    }
    out = build_env_file(_arch(), template_env)

    # Each prefix gets a comment header
    assert "# POSTGRES" in out
    assert "# REDIS" in out
    assert "# FUSIONAUTH" in out

    # Vars present in their groups
    assert "POSTGRES_DB=pet_groomer" in out
    assert "REDIS_URL=redis://redis:6379/0" in out
    assert "FUSIONAUTH_API_KEY=abc" in out


def test_env_has_trailing_newline():
    out = build_env_file(_arch(), {"FOO_BAR": "baz"})
    assert out.endswith("\n")


def test_empty_env_just_has_project_metadata():
    out = build_env_file(_arch(), {})
    assert "PROJECT_NAME=" in out
    assert "Pet Groomer" in out

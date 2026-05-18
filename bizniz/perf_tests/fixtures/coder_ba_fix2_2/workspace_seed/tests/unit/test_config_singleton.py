"""Verify the module-level ``settings`` singleton is exported from app.core.config.

BE-001-U2 deliverable: consumers must be able to ``from app.core.config import settings``
and receive a fully-constructed Settings instance whose FusionAuth fields reflect
the process environment.
"""
from app.core import config as config_module
from app.core.config import Settings, settings


def test_settings_singleton_is_module_attribute() -> None:
    """``settings`` is bound at module scope."""
    assert hasattr(config_module, "settings")


def test_settings_singleton_is_settings_instance() -> None:
    """The singleton is an instance of the Settings class (not a factory)."""
    assert isinstance(settings, Settings)


def test_settings_singleton_carries_fusionauth_fields() -> None:
    """U1 field additions flow through to the singleton."""
    assert hasattr(settings, "fusionauth_url")
    assert hasattr(settings, "fusionauth_application_id")
    assert hasattr(settings, "fusionauth_tenant_id")
    assert hasattr(settings, "fusionauth_issuer")
    assert hasattr(settings, "fusionauth_api_key")
    assert hasattr(settings, "jwt_leeway_seconds")


def test_settings_singleton_is_stable_across_imports() -> None:
    """Re-importing yields the same cached instance (lru_cache on get_settings)."""
    from app.core.config import settings as settings_again
    assert settings is settings_again

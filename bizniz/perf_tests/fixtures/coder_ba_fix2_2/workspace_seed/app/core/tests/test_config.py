"""Unit tests for FusionAuth-related fields on ``app.core.config.Settings``.

These tests are hermetic: they wipe the FusionAuth/JWT env vars first via
``monkeypatch.delenv`` and then set only what each test needs via
``monkeypatch.setenv``. This guarantees the assertions reflect the
Settings class defaults (not whatever the host shell or compose env
happens to inject at runtime).
"""
from app.core.config import Settings, settings


_FUSIONAUTH_ENV_VARS = (
    "FUSIONAUTH_URL",
    "FUSIONAUTH_APPLICATION_ID",
    "FUSIONAUTH_TENANT_ID",
    "FUSIONAUTH_ISSUER",
    "FUSIONAUTH_API_KEY",
    "JWT_LEEWAY_SECONDS",
)


def _scrub_env(monkeypatch) -> None:
    """Remove every FusionAuth/JWT env var so Settings() sees a clean slate."""
    for name in _FUSIONAUTH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


def test_fusionauth_defaults(monkeypatch) -> None:
    """Defaults apply when only the required UUID fields are provided."""
    _scrub_env(monkeypatch)
    monkeypatch.setenv("FUSIONAUTH_APPLICATION_ID", "85a03867-dccf-4882-adde-1a79aeec50df")
    monkeypatch.setenv("FUSIONAUTH_TENANT_ID", "d4465dd9-12e7-4715-bc4e-690874974b6b")
    # api_key is also required by the current Settings shape — give it a value
    # so Settings() can construct; the assertions below cover the defaulted fields.
    monkeypatch.setenv("FUSIONAUTH_API_KEY", "test-api-key")

    s = Settings()

    assert s.fusionauth_url == "http://auth:9011"
    assert s.fusionauth_issuer == "acme.com"
    assert s.jwt_leeway_seconds == 60
    assert s.fusionauth_application_id == "85a03867-dccf-4882-adde-1a79aeec50df"
    assert s.fusionauth_tenant_id == "d4465dd9-12e7-4715-bc4e-690874974b6b"


def test_fusionauth_env_override(monkeypatch) -> None:
    """Each FusionAuth field reflects its env override when set explicitly."""
    _scrub_env(monkeypatch)
    monkeypatch.setenv("FUSIONAUTH_URL", "http://custom-fa:9011")
    monkeypatch.setenv("FUSIONAUTH_APPLICATION_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("FUSIONAUTH_TENANT_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("FUSIONAUTH_ISSUER", "custom-issuer.example.com")
    monkeypatch.setenv("FUSIONAUTH_API_KEY", "custom-api-key")
    monkeypatch.setenv("JWT_LEEWAY_SECONDS", "120")

    s = Settings()

    assert s.fusionauth_url == "http://custom-fa:9011"
    assert s.fusionauth_application_id == "11111111-1111-1111-1111-111111111111"
    assert s.fusionauth_tenant_id == "22222222-2222-2222-2222-222222222222"
    assert s.fusionauth_issuer == "custom-issuer.example.com"
    assert s.fusionauth_api_key == "custom-api-key"
    assert s.jwt_leeway_seconds == 120


def test_settings_singleton_is_settings_instance() -> None:
    """``from app.core.config import settings`` yields a Settings instance."""
    assert isinstance(settings, Settings)

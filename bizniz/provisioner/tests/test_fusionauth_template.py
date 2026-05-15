"""Tests for the FusionAuth infrastructure template."""
import json
from pathlib import Path

from bizniz.architect.types import ServiceDefinition
from bizniz.provisioner.templates import FusionAuthTemplate
from bizniz.provisioner.templates.base import TemplateContext


def _service(port: int | None = 9011) -> ServiceDefinition:
    return ServiceDefinition(
        name="auth",
        service_type="auth",
        framework="fusionauth",
        language="yaml",
        description="oauth/oidc identity provider",
        workspace_name="fusionauth",
        port=port,
        depends_on=[],
        requirements=[],
        skeleton="none",
    )


def _ctx(svc, slug="petgroomer") -> TemplateContext:
    return TemplateContext(service=svc, project_slug=slug, project_root=Path("/tmp"))


def test_compose_uses_official_image():
    out = FusionAuthTemplate().render(_ctx(_service()))
    assert out.compose_service["image"] == "fusionauth/fusionauth-app:latest"


def test_compose_depends_on_postgres_healthy():
    out = FusionAuthTemplate().render(_ctx(_service()))
    deps = out.compose_service["depends_on"]
    assert "postgres" in deps
    assert deps["postgres"]["condition"] == "service_healthy"


def test_compose_depends_on_renamed_postgres_service():
    """The architect can name the postgres service anything.
    FusionAuth must depend on whatever it's actually called — not hardcode "postgres".
    """
    from bizniz.architect.types import SystemArchitecture

    pg = ServiceDefinition(
        name="data",  # ← architect's choice, not "postgres"
        service_type="database", framework="postgres", language="sql",
        description="primary db", workspace_name="postgres", port=5432,
        depends_on=[], requirements=[], skeleton="none",
    )
    auth = _service()
    arch = SystemArchitecture(
        project_name="P", project_slug="p", description="d",
        services=[pg, auth],
    )
    ctx = TemplateContext(
        service=auth, project_slug="p", project_root=Path("/tmp"),
        architecture=arch,
    )
    out = FusionAuthTemplate().render(ctx)
    assert "data" in out.compose_service["depends_on"]
    assert "postgres" not in out.compose_service["depends_on"]
    assert out.depends_on_services == ["data"]
    assert "data:5432" in out.compose_service["environment"]["DATABASE_URL"]


def test_kickstart_file_exists_and_is_valid_json():
    out = FusionAuthTemplate().render(_ctx(_service()))
    path = "fusionauth/kickstart/kickstart.json"
    assert path in out.infra_files
    parsed = json.loads(out.infra_files[path])
    # Top-level kickstart shape
    assert "variables" in parsed
    assert "apiKeys" in parsed
    assert "requests" in parsed


def test_kickstart_creates_admin_application_and_roles():
    out = FusionAuthTemplate().render(_ctx(_service()))
    parsed = json.loads(out.infra_files["fusionauth/kickstart/kickstart.json"])
    requests = parsed["requests"]

    # An application creation request should exist
    app_creates = [
        r for r in requests
        if r.get("method") == "POST" and "/api/application/" in r.get("url", "")
    ]
    assert len(app_creates) >= 1
    app_body = app_creates[0]["body"]["application"]

    role_names = {r["name"] for r in app_body["roles"]}
    assert "admin" in role_names
    assert "user" in role_names

    # Admin email must be a valid hostname in the domain part — no
    # underscores. Sanitization is exercised by the dedicated tests
    # ``test_admin_email_sanitizes_underscore_slug`` /
    # ``test_admin_email_handles_pathological_slug``.
    admin_email = parsed["variables"]["adminEmail"]
    domain = admin_email.split("@", 1)[1]
    assert "_" not in domain, f"FA rejects underscores in email domain: {admin_email!r}"

    # Admin user registration must exist — TWO of them: one for the
    # project's app, one for FA's built-in system app (so the
    # setup-wizard redirect is bypassed).
    user_regs = [
        r for r in requests
        if r.get("method") == "POST" and "/api/user/registration/" in r.get("url", "")
    ]
    assert len(user_regs) == 2
    # First registration: project app, role=admin, and creates the user.
    project_reg = user_regs[0]
    assert "user" in project_reg["body"]  # user is created here
    assert "admin" in project_reg["body"]["registration"]["roles"]
    assert project_reg["body"]["registration"]["applicationId"] == "#{applicationId}"
    # Second registration: FA system app — no user body (the user
    # already exists from the first call), but with admin role so FA
    # treats them as a system admin and skips the setup wizard.
    system_reg = user_regs[1]
    assert "user" not in system_reg["body"]
    assert system_reg["body"]["registration"]["applicationId"] == "#{systemAppId}"
    assert "admin" in system_reg["body"]["registration"]["roles"]


def test_kickstart_sets_oauth_redirect_for_frontends():
    out = FusionAuthTemplate().render(_ctx(_service()))
    parsed = json.loads(out.infra_files["fusionauth/kickstart/kickstart.json"])
    app_create = next(
        r for r in parsed["requests"]
        if r.get("method") == "POST" and "/api/application/" in r.get("url", "")
    )
    redirects = app_create["body"]["application"]["oauthConfiguration"]["authorizedRedirectURLs"]
    # React (5173) and Angular (4200) frontends are both supported by default
    assert any(":5173" in url for url in redirects)
    assert any(":4200" in url for url in redirects)


def test_env_vars_include_admin_credentials_and_api_key():
    out = FusionAuthTemplate().render(_ctx(_service(), slug="myproj"))
    env = out.env_vars
    assert env["FUSIONAUTH_ADMIN_EMAIL"].endswith("@myproj.local")
    assert env["FUSIONAUTH_ADMIN_PASSWORD"]
    assert env["FUSIONAUTH_API_KEY"]
    assert env["FUSIONAUTH_APPLICATION_ID"]
    assert env["FUSIONAUTH_ISSUER"].startswith("http://")


def test_depends_on_services_includes_postgres():
    """The provisioner uses this hint to ensure postgres is present."""
    out = FusionAuthTemplate().render(_ctx(_service()))
    assert "postgres" in out.depends_on_services


def test_kickstart_volume_mount():
    out = FusionAuthTemplate().render(_ctx(_service()))
    volumes = out.compose_service["volumes"]
    # Read-only mount of the kickstart dir into the FusionAuth path
    assert any("kickstart" in v and ":ro" in v for v in volumes)


def test_admin_email_sanitizes_underscore_slug():
    """Slugs with underscores produce a hostname-invalid domain in the
    admin email. FA's email validator rejects these with
    ``[notEmail]user.email`` and the entire kickstart fails. Sanitizer
    must replace underscores with hyphens."""
    out = FusionAuthTemplate().render(_ctx(_service(), slug="recipe_box"))
    parsed = json.loads(out.infra_files["fusionauth/kickstart/kickstart.json"])
    email = parsed["variables"]["adminEmail"]
    assert email == "admin@recipe-box.local"


def test_admin_email_handles_pathological_slug():
    """Slugs with no hostname-valid characters should still produce
    a working email (fallback domain)."""
    out = FusionAuthTemplate().render(_ctx(_service(), slug="___"))
    parsed = json.loads(out.infra_files["fusionauth/kickstart/kickstart.json"])
    email = parsed["variables"]["adminEmail"]
    domain = email.split("@", 1)[1]
    assert "_" not in domain
    assert domain  # not empty

"""
FusionAuth infrastructure template.

FusionAuth is the project's default OAuth/OIDC provider. The template
emits:

  - docker-compose service entry (depends on postgres)
  - kickstart.json that pre-configures a tenant, application, roles, an
    initial admin user, and OAuth redirect URLs derived from the project's
    frontend service
  - .env entries for FusionAuth admin password, API key, app id, and
    issuer URL the application services use

The kickstart file is FusionAuth's bootstrapping format — it executes
the listed REST requests once on first start, creating the realm
configuration without any manual UI clicks. Kickstart docs:
https://fusionauth.io/docs/v1/tech/installation-guide/kickstart
"""
from __future__ import annotations

import json

from bizniz.provisioner.templates.base import (
    InfraTemplate,
    TemplateContext,
    TemplateOutput,
)


class FusionAuthTemplate(InfraTemplate):

    DEFAULT_CONTAINER_PORT = 9011

    # Stable UUIDs so kickstart is idempotent across re-runs of a project.
    # Customize per-project by deriving from project_slug if you want.
    APPLICATION_ID = "85a03867-dccf-4882-adde-1a79aeec50df"
    ADMIN_USER_ID = "00000000-0000-0000-0000-000000000001"
    # Stable RSA signing key ID. Kickstart generates an RS256 keypair
    # at this ID and binds it as the tenant's accessTokenSigningKey
    # (see comment on the PATCH /api/tenant request below). Without
    # this, FusionAuth defaults to HS256 → JWKS exposes no public
    # keys → backend's RS256 + JWKS validation fails on every JWT.
    ACCESS_TOKEN_KEY_ID = "12345678-1234-1234-1234-123456789012"
    # FusionAuth ships with this default tenant ID built-in. We do NOT
    # set this as a kickstart variable — kickstart treats `defaultTenantId`
    # specially (a "rename the default tenant" trigger), and renaming it
    # to itself fails with a tenants_pkey unique-constraint violation.
    # Instead, reference the UUID literally in PATCH URLs.
    DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000000"

    def render(self, ctx: TemplateContext) -> TemplateOutput:
        host_port = ctx.service.port or self.DEFAULT_CONTAINER_PORT
        slug = ctx.project_slug
        # The architect can name the postgres service anything ("db",
        # "postgres", "data") — look up the actual name and use it for
        # depends_on + the JDBC hostname so this template works
        # regardless of naming convention.
        pg = ctx.find_by_framework("postgres")
        pg_name = pg.name if pg is not None else "postgres"
        own_name = ctx.service.name
        # Dev defaults; the project owner replaces these in .env for prod.
        admin_email = f"admin@{slug}.local"
        admin_password = "ChangeMe123!"
        api_key = "bf69486b-4733-4470-a592-f1bfce7af580"
        issuer = f"http://{own_name}:{self.DEFAULT_CONTAINER_PORT}"

        kickstart = {
            "variables": {
                "applicationId": self.APPLICATION_ID,
                "adminUserId": self.ADMIN_USER_ID,
                "adminEmail": admin_email,
                "adminPassword": admin_password,
                "apiKey": api_key,
                "appName": slug,
                "accessTokenKeyId": self.ACCESS_TOKEN_KEY_ID,
            },
            "apiKeys": [
                {
                    "key": "#{apiKey}",
                    "description": "Bizniz bootstrap key",
                }
            ],
            "requests": [
                # Generate an RSA-2048 keypair for the tenant's access
                # tokens. Without this, FA defaults to an HMAC SHA-256
                # key, JWKS endpoint exposes no public keys, and the
                # skeleton's RS256 + JWKS validation fails on every JWT
                # ("Signature verification failed"). RS256 is the
                # standard for JWKS-based service-to-service token
                # validation; HS256 would require shared-secret
                # distribution.
                {
                    "method": "POST",
                    "url": "/api/key/generate/#{accessTokenKeyId}",
                    "body": {
                        "key": {
                            "algorithm": "RS256",
                            "name": "Access Token Signing Key",
                            "length": 2048,
                        }
                    },
                },
                # NOTE: we deliberately do NOT PATCH the default
                # tenant here. FusionAuth's PATCH validator on a
                # freshly-bootstrapped default tenant is broken in
                # both directions:
                #   - body without name → 400 [blank]tenant.name
                #   - body with name="Default" → 400 [duplicate]tenant.name
                # The application-level jwtConfiguration below
                # overrides the tenant's defaults and IS accepted on
                # PATCH/POST. JWT signing config goes there.
                #
                # Tenant.issuer would be nice to set (so the JWT's
                # ``iss`` claim matches the backend's
                # ``FUSIONAUTH_ISSUER``), but the same validator
                # quirk blocks it. Skeleton's auth.py is tolerant of
                # the default issuer in dev mode.
                # Roles for the application
                {
                    "method": "POST",
                    "url": "/api/application/#{applicationId}",
                    "body": {
                        "application": {
                            "name": "#{appName}",
                            "roles": [
                                {"name": "admin", "isSuperRole": True},
                                {"name": "user", "isDefault": True},
                            ],
                            "oauthConfiguration": {
                                "authorizedRedirectURLs": [
                                    "http://localhost:5173/auth/callback",
                                    "http://localhost:4200/auth/callback",
                                ],
                                "logoutURL": "http://localhost:5173/logout",
                                "requireRegistration": True,
                                "generateRefreshTokens": True,
                                "enabledGrants": [
                                    "authorization_code",
                                    "refresh_token",
                                ],
                            },
                            "jwtConfiguration": {
                                "enabled": True,
                                "timeToLiveInSeconds": 3600,
                                "refreshTokenTimeToLiveInMinutes": 43200,
                                # Bind the RSA key here at the
                                # APPLICATION level (not tenant) —
                                # FA's tenant PATCH validator is
                                # broken on fresh tenants. Application
                                # JWT config overrides tenant defaults
                                # and the validator is happy here.
                                "accessTokenKeyId": "#{accessTokenKeyId}",
                                "idTokenKeyId": "#{accessTokenKeyId}",
                            },
                        }
                    },
                },
                # Admin user with admin role
                {
                    "method": "POST",
                    "url": "/api/user/registration/#{adminUserId}",
                    "body": {
                        "user": {
                            "email": "#{adminEmail}",
                            "password": "#{adminPassword}",
                        },
                        "registration": {
                            "applicationId": "#{applicationId}",
                            "roles": ["admin"],
                        },
                    },
                },
            ],
        }

        compose_service = {
            "image": "fusionauth/fusionauth-app:latest",
            "depends_on": {
                pg_name: {"condition": "service_healthy"},
            },
            "environment": {
                "DATABASE_URL": f"jdbc:postgresql://{pg_name}:5432/fusionauth",
                "DATABASE_ROOT_USERNAME": "${POSTGRES_USER}",
                "DATABASE_ROOT_PASSWORD": "${POSTGRES_PASSWORD}",
                "DATABASE_USERNAME": "${POSTGRES_USER}",
                "DATABASE_PASSWORD": "${POSTGRES_PASSWORD}",
                "FUSIONAUTH_APP_RUNTIME_MODE": "development",
                "FUSIONAUTH_APP_KICKSTART_FILE":
                    "/usr/local/fusionauth/kickstart/kickstart.json",
            },
            "ports": [f"{host_port}:9011"],
            "volumes": [
                "./fusionauth/kickstart:/usr/local/fusionauth/kickstart:ro",
            ],
            "networks": ["app-network"],
        }

        env_vars = {
            "FUSIONAUTH_URL": issuer,  # internal Docker URL for backend → FusionAuth
            "FUSIONAUTH_ADMIN_EMAIL": admin_email,
            "FUSIONAUTH_ADMIN_PASSWORD": admin_password,
            "FUSIONAUTH_API_KEY": api_key,
            "FUSIONAUTH_APPLICATION_ID": self.APPLICATION_ID,
            "FUSIONAUTH_ISSUER": issuer,
            "FUSIONAUTH_HOST_URL": f"http://localhost:{host_port}",
        }

        return TemplateOutput(
            compose_service=compose_service,
            compose_networks=["app-network"],
            infra_files={
                "fusionauth/kickstart/kickstart.json":
                    json.dumps(kickstart, indent=2) + "\n",
            },
            env_vars=env_vars,
            depends_on_services=[pg_name],
        )

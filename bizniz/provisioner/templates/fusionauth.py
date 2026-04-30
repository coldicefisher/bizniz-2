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
    DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000000"

    def render(self, ctx: TemplateContext) -> TemplateOutput:
        host_port = ctx.service.port or self.DEFAULT_CONTAINER_PORT
        slug = ctx.project_slug
        # Dev defaults; the project owner replaces these in .env for prod.
        admin_email = f"admin@{slug}.local"
        admin_password = "ChangeMe123!"
        api_key = "bf69486b-4733-4470-a592-f1bfce7af580"
        issuer = f"http://fusionauth:{self.DEFAULT_CONTAINER_PORT}"

        kickstart = {
            "variables": {
                "defaultTenantId": self.DEFAULT_TENANT_ID,
                "applicationId": self.APPLICATION_ID,
                "adminUserId": self.ADMIN_USER_ID,
                "adminEmail": admin_email,
                "adminPassword": admin_password,
                "apiKey": api_key,
                "appName": slug,
            },
            "apiKeys": [
                {
                    "key": "#{apiKey}",
                    "description": "Bizniz bootstrap key",
                }
            ],
            "requests": [
                # Patch the default tenant — issuer must match how the
                # application services will see FusionAuth from inside the
                # docker network.
                {
                    "method": "PATCH",
                    "url": "/api/tenant/#{defaultTenantId}",
                    "body": {
                        "tenant": {
                            "issuer": issuer,
                        }
                    },
                },
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
                "postgres": {"condition": "service_healthy"},
            },
            "environment": {
                "DATABASE_URL": "jdbc:postgresql://postgres:5432/fusionauth",
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
            depends_on_services=["postgres"],
        )

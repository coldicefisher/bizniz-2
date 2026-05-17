"""Tests for deterministic doc renderers (8B)."""
from __future__ import annotations

import textwrap

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.documenters.human_docs.deterministic import (
    render_api_reference,
    render_architecture,
    render_auth_pointer,
    render_infrastructure,
)


def _svc(name, type_, framework="?", language="?", port=None,
         depends_on=None) -> ServiceDefinition:
    return ServiceDefinition(
        name=name, service_type=type_, framework=framework,
        language=language, description="d", workspace_name=name,
        port=port, depends_on=depends_on or [],
    )


def _arch(*services, name="My App", slug="my_app",
          description="A useful app") -> SystemArchitecture:
    return SystemArchitecture(
        project_name=name, project_slug=slug, description=description,
        services=list(services),
    )


# ── architecture.md ──────────────────────────────────────────────


class TestRenderArchitecture:
    def test_includes_project_name_and_slug(self):
        out = render_architecture(_arch(_svc("backend", "backend")))
        assert "# Architecture — My App" in out
        assert "my_app" in out

    def test_includes_description(self):
        out = render_architecture(_arch(_svc("backend", "backend")))
        assert "A useful app" in out

    def test_embeds_mermaid_graph(self):
        out = render_architecture(_arch(_svc("backend", "backend")))
        assert "```mermaid" in out
        assert "graph TD" in out

    def test_service_table_rows(self):
        out = render_architecture(_arch(
            _svc("backend", "backend", framework="fastapi",
                 language="python", port=8000),
            _svc("frontend", "frontend", framework="react",
                 language="typescript", port=5173),
        ))
        assert "| backend | backend | fastapi | python | 8000 |" in out
        assert "| frontend | frontend | react | typescript | 5173 |" in out

    def test_environments_stub_present(self):
        out = render_architecture(_arch(_svc("backend", "backend")))
        assert "## Environments" in out
        assert "development" in out
        # Stub references future enrichment.
        assert "staging" in out.lower() or "tbd" in out.lower()

    def test_auth_link_when_auth_service_present(self):
        out = render_architecture(_arch(
            _svc("backend", "backend"),
            _svc("auth", "auth", framework="fusionauth"),
        ))
        assert "auth.md" in out
        assert "fusionauth" in out

    def test_no_auth_link_when_no_auth_service(self):
        out = render_architecture(_arch(_svc("backend", "backend")))
        # No auth services → no "Authentication" line.
        assert "Authentication:" not in out


# ── infrastructure.md ────────────────────────────────────────────


_SAMPLE_COMPOSE = """
name: my_app
services:
  backend:
    image: my_app-backend:dev
    ports:
      - "8001:8000"
    volumes:
      - ../../backend:/app
      - ../../core/python:/python_core
    depends_on:
      db:
        condition: service_healthy
  db:
    image: postgres:16-alpine
    ports:
      - "5432:5432"
volumes:
  pgdata:
networks:
  app-network:
"""


class TestRenderInfrastructure:
    def test_lists_services_table(self):
        arch = _arch(_svc("backend", "backend"))
        out = render_infrastructure(_SAMPLE_COMPOSE, arch)
        assert "| backend |" in out
        assert "my_app-backend:dev" in out
        assert "8001:8000" in out

    def test_includes_networks_section(self):
        out = render_infrastructure(_SAMPLE_COMPOSE, _arch(_svc("x", "backend")))
        assert "## Networks" in out
        assert "app-network" in out

    def test_includes_volumes_section(self):
        out = render_infrastructure(_SAMPLE_COMPOSE, _arch(_svc("x", "backend")))
        assert "## Top-level volumes" in out
        assert "pgdata" in out

    def test_includes_core_mount_section(self):
        out = render_infrastructure(_SAMPLE_COMPOSE, _arch(_svc("x", "backend")))
        assert "core library" in out.lower()
        assert "python_core" in out

    def test_dep_block_dict_form(self):
        # When depends_on is `{db: {condition: ...}}` style.
        out = render_infrastructure(_SAMPLE_COMPOSE, _arch(_svc("x", "backend")))
        assert "db" in out

    def test_empty_compose_does_not_crash(self):
        out = render_infrastructure("", _arch(_svc("x", "backend")))
        assert "# Infrastructure" in out

    def test_malformed_yaml_does_not_crash(self):
        out = render_infrastructure(":::not yaml", _arch(_svc("x", "backend")))
        assert "# Infrastructure" in out


# ── api/<service>.md ─────────────────────────────────────────────


_SAMPLE_OPENAPI = {
    "info": {"title": "CRM API", "version": "0.1.0"},
    "paths": {
        "/health": {
            "get": {
                "summary": "Health check", "tags": ["meta"],
            },
        },
        "/api/v1/companies": {
            "get": {
                "summary": "List companies",
                "tags": ["companies"],
                "security": [{"bearerAuth": []}],
            },
            "post": {
                "summary": "Create company",
                "tags": ["companies"],
                "security": [{"bearerAuth": []}],
            },
        },
    },
}


class TestRenderApiReference:
    def test_includes_service_metadata(self):
        out = render_api_reference("backend", _SAMPLE_OPENAPI)
        assert "# API Reference — backend" in out
        assert "CRM API" in out
        assert "0.1.0" in out

    def test_lists_endpoints(self):
        out = render_api_reference("backend", _SAMPLE_OPENAPI)
        assert "`GET`" in out
        assert "`POST`" in out
        assert "`/health`" in out
        assert "`/api/v1/companies`" in out
        assert "List companies" in out

    def test_marks_auth_required_routes(self):
        out = render_api_reference("backend", _SAMPLE_OPENAPI)
        # /api/v1/companies routes have security
        assert "🔒 yes" in out

    def test_no_paths_emits_friendly_message(self):
        out = render_api_reference(
            "backend",
            {"info": {"title": "Empty", "version": "1.0"}, "paths": {}},
        )
        assert "_No paths captured._" in out


# ── auth.md ──────────────────────────────────────────────────────


class TestRenderAuthPointer:
    def test_points_to_root_contract(self):
        out = render_auth_pointer(_arch(_svc("backend", "backend")))
        assert "AUTH_CONTRACT.md" in out
        # Default is relative to docs/.
        assert "../AUTH_CONTRACT.md" in out

    def test_lists_auth_services_section_when_present(self):
        out = render_auth_pointer(_arch(
            _svc("backend", "backend"),
            _svc("auth", "auth", framework="fusionauth", port=9011),
        ))
        assert "## Auth services" in out
        assert "fusionauth" in out
        assert "9011" in out

    def test_lists_consumer_services(self):
        out = render_auth_pointer(_arch(
            _svc("backend", "backend"),
            _svc("worker", "worker"),  # not backend → not a consumer
        ))
        # backend is a consumer; worker is not.
        assert "**backend**" in out
        # No worker line.
        assert "**worker**" not in out

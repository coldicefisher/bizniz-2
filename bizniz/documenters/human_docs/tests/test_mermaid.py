"""Tests for the Mermaid service-graph renderer."""
from __future__ import annotations

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.documenters.human_docs.mermaid import (
    _safe_id, render_service_graph,
)


def _arch(*services) -> SystemArchitecture:
    return SystemArchitecture(
        project_name="X", project_slug="x", description="",
        services=list(services),
    )


def _svc(name, type_, framework="?", depends_on=None,
         language="?", port=None) -> ServiceDefinition:
    return ServiceDefinition(
        name=name, service_type=type_, framework=framework,
        language=language, description="d", workspace_name=name,
        port=port, depends_on=depends_on or [],
    )


class TestSafeId:
    @pytest.mark.parametrize("inp,expected", [
        ("backend", "backend"),
        ("foo-bar", "foo_bar"),
        ("svc with space", "svc_with_space"),
        ("svc.v2", "svc_v2"),
    ])
    def test_sanitization(self, inp, expected):
        assert _safe_id(inp) == expected


class TestRenderServiceGraph:
    def test_emits_fenced_block(self):
        arch = _arch(_svc("a", "backend"))
        out = render_service_graph(arch)
        assert out.startswith("```mermaid")
        assert out.endswith("```")
        assert "graph TD" in out

    def test_includes_every_service_as_node(self):
        arch = _arch(
            _svc("backend", "backend", framework="fastapi"),
            _svc("frontend", "frontend", framework="react"),
            _svc("db", "database", framework="postgres"),
        )
        out = render_service_graph(arch)
        assert "backend" in out
        assert "frontend" in out
        assert "db" in out
        assert "fastapi" in out
        assert "postgres" in out

    def test_emits_dependency_edges(self):
        arch = _arch(
            _svc("frontend", "frontend", depends_on=["backend"]),
            _svc("backend", "backend", depends_on=["db", "auth"]),
            _svc("db", "database"),
            _svc("auth", "auth"),
        )
        out = render_service_graph(arch)
        assert "frontend --> backend" in out
        assert "backend --> db" in out
        assert "backend --> auth" in out

    def test_unknown_dependency_skipped(self):
        # Defensive: if depends_on references a service that doesn't
        # exist in the architecture, drop the edge rather than emit
        # a Mermaid syntax error.
        arch = _arch(
            _svc("backend", "backend", depends_on=["ghost"]),
        )
        out = render_service_graph(arch)
        assert "ghost" not in out

    def test_isolated_services_still_appear(self):
        arch = _arch(_svc("orphan", "worker"))
        out = render_service_graph(arch)
        assert "orphan" in out

    def test_hyphenated_service_names_sanitized(self):
        arch = _arch(_svc("my-svc", "backend", depends_on=["db-pg"]))
        arch.services.append(_svc("db-pg", "database"))
        out = render_service_graph(arch)
        # Node IDs are sanitized but display labels keep the hyphen.
        assert "my_svc" in out
        assert "db_pg" in out
        assert "my-svc" in out   # in the display string
        assert "my_svc --> db_pg" in out

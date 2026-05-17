"""Tests for the LLM-driven narrative writer (8B)."""
from __future__ import annotations

from typing import List, Optional

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.documenters.human_docs.llm_narrative import (
    NarrativeResult,
    NarrativeWriter,
    _fallback_stub,
)


def _arch() -> SystemArchitecture:
    return SystemArchitecture(
        project_name="My App", project_slug="my_app",
        description="A useful app.",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend",
                framework="fastapi", language="python",
                description="REST API", workspace_name="backend",
                port=8000,
            ),
            ServiceDefinition(
                name="frontend", service_type="frontend",
                framework="react", language="typescript",
                description="SPA", workspace_name="frontend",
                port=5173,
            ),
        ],
    )


class TestFallbackStub:
    def test_includes_label(self):
        s = _fallback_stub("README.md", "LLM call timed out")
        assert "README.md" in s
        assert "LLM call timed out" in s


class TestNarrativeWriter:
    def test_write_readme_success(self):
        captured: List[tuple] = []
        def fake(sys_p, user_p):
            captured.append((sys_p, user_p))
            return "# My App\n\nA useful app."
        writer = NarrativeWriter(llm_invoker=fake)
        result = writer.write_readme(_arch(), problem_statement="Build a CRM")
        assert result.succeeded is True
        assert "My App" in result.content
        # The user prompt got the problem statement.
        assert "Build a CRM" in captured[0][1]
        # And the service list.
        assert "backend" in captured[0][1]
        assert "frontend" in captured[0][1]

    def test_write_readme_empty_returns_stub(self):
        writer = NarrativeWriter(llm_invoker=lambda s, u: "")
        result = writer.write_readme(_arch())
        assert result.succeeded is False
        assert "README.md" in result.content
        assert "(Auto-generation" in result.content

    def test_write_readme_none_returns_stub(self):
        writer = NarrativeWriter(llm_invoker=lambda s, u: None)
        result = writer.write_readme(_arch())
        assert result.succeeded is False
        assert "README.md" in result.content

    def test_write_readme_llm_exception_returns_stub(self):
        def boom(s, u):
            raise RuntimeError("network down")
        writer = NarrativeWriter(llm_invoker=boom)
        result = writer.write_readme(_arch())
        assert result.succeeded is False
        assert "RuntimeError" in result.content or "network" in result.content.lower()

    def test_write_quickstart_includes_ports(self):
        prompts: List[str] = []
        def fake(s, u):
            prompts.append(u)
            return "# Quickstart\n"
        writer = NarrativeWriter(llm_invoker=fake)
        writer.write_quickstart(_arch())
        assert "port 8000" in prompts[0]
        assert "port 5173" in prompts[0]

    def test_write_service_includes_metadata(self):
        prompts: List[str] = []
        def fake(s, u):
            prompts.append(u)
            return "# backend\n"
        writer = NarrativeWriter(llm_invoker=fake)
        arch = _arch()
        writer.write_service(arch.services[0], arch)
        assert "backend" in prompts[0]
        assert "fastapi" in prompts[0]
        assert "8000" in prompts[0]

    def test_write_milestone_includes_index_and_tag(self):
        prompts: List[str] = []
        def fake(s, u):
            prompts.append(u)
            return "# M1\n"
        writer = NarrativeWriter(llm_invoker=fake)
        writer.write_milestone(
            milestone_index=3,
            milestone_name="Companies CRUD",
            milestone_problem_slice="Add companies entity",
            capabilities_summary="Create/list/edit/delete companies",
        )
        assert "Milestone 3" in prompts[0]
        assert "Companies CRUD" in prompts[0]
        assert "m3-done" in prompts[0]

    def test_status_callback_emits_progress(self):
        statuses: List[str] = []
        writer = NarrativeWriter(
            on_status=lambda m: statuses.append(m),
            llm_invoker=lambda s, u: "# X",
        )
        writer.write_readme(_arch())
        joined = " ".join(statuses)
        assert "README" in joined

    def test_status_callback_does_not_crash_on_buggy(self):
        def boom(_):
            raise RuntimeError("logger broke")
        writer = NarrativeWriter(
            on_status=boom,
            llm_invoker=lambda s, u: "# X",
        )
        result = writer.write_readme(_arch())
        assert result.succeeded is True

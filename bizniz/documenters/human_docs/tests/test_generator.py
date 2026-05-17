"""Tests for the ``HumanDocsGenerator`` orchestrator (8B)."""
from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.documenters.human_docs.generator import (
    GeneratedDoc,
    HumanDocsGenerator,
    HumanDocsResult,
    MilestoneDocInput,
)
from bizniz.documenters.human_docs.llm_narrative import (
    NarrativeResult,
    NarrativeWriter,
)


def _arch(*services) -> SystemArchitecture:
    if not services:
        services = (
            ServiceDefinition(
                name="backend", service_type="backend",
                framework="fastapi", language="python",
                description="API", workspace_name="backend", port=8000,
            ),
            ServiceDefinition(
                name="frontend", service_type="frontend",
                framework="react", language="typescript",
                description="SPA", workspace_name="frontend", port=5173,
            ),
            ServiceDefinition(
                name="db", service_type="database",
                framework="postgres", language="sql",
                description="DB", workspace_name="db", port=5432,
            ),
        )
    return SystemArchitecture(
        project_name="My App", project_slug="my_app",
        description="A useful app.", services=list(services),
    )


def _fake_writer(content_by_method: dict = None) -> NarrativeWriter:
    """NarrativeWriter that returns a canned non-empty string for
    each invoke. ``content_by_method`` overrides specific outputs."""
    content_by_method = content_by_method or {}
    counter = {"i": 0}
    def fake(sys_p, user_p):
        counter["i"] += 1
        return f"# Narrative {counter['i']}\n\nbody"
    return NarrativeWriter(llm_invoker=fake)


class TestGenerator:
    def test_runs_end_to_end_on_minimal_arch(self, tmp_path):
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=_fake_writer(),
            compose_yaml="services: {}",
            openapi_per_service={
                "backend": {
                    "info": {"title": "X", "version": "0.1"},
                    "paths": {"/health": {"get": {"summary": "h"}}},
                },
            },
            problem_statement="Build a CRM",
            milestones=[
                MilestoneDocInput(index=1, name="Auth"),
                MilestoneDocInput(index=2, name="CRUD"),
            ],
        )
        result = gen.run()
        assert result.passed is True
        # Every doc file should exist.
        rels = {d.rel_path for d in result.docs}
        assert "architecture.md" in rels
        assert "infrastructure.md" in rels
        assert "auth.md" in rels
        assert "api/backend.md" in rels
        assert "README.md" in rels
        assert "quickstart.md" in rels
        assert "services/backend.md" in rels
        assert "services/frontend.md" in rels
        assert "milestones/m1.md" in rels
        assert "milestones/m2.md" in rels

    def test_files_actually_written_to_disk(self, tmp_path):
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=_fake_writer(),
            compose_yaml="",
        )
        result = gen.run()
        for doc in result.docs:
            path = tmp_path / "docs" / doc.rel_path
            assert path.is_file(), f"{doc.rel_path} not written"
            assert path.read_text(encoding="utf-8")  # non-empty

    def test_infrastructure_only_services_excluded_from_service_docs(self, tmp_path):
        # db / cache should NOT get a services/<name>.md (they're
        # infrastructure, covered by infrastructure.md).
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=_fake_writer(),
        )
        result = gen.run()
        rels = {d.rel_path for d in result.docs}
        assert "services/backend.md" in rels
        assert "services/frontend.md" in rels
        assert "services/db.md" not in rels

    def test_no_openapi_no_api_docs(self, tmp_path):
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=_fake_writer(),
            openapi_per_service={},
        )
        result = gen.run()
        rels = {d.rel_path for d in result.docs}
        # No api/<svc>.md created when no openapi captured.
        assert not any(r.startswith("api/") for r in rels)

    def test_no_milestones_no_milestone_docs(self, tmp_path):
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=_fake_writer(),
            milestones=[],
        )
        result = gen.run()
        rels = {d.rel_path for d in result.docs}
        assert not any(r.startswith("milestones/") for r in rels)

    def test_llm_failure_records_per_doc_but_does_not_halt(self, tmp_path):
        # README fails, others succeed.
        responses = iter([
            "",                     # README — empty → stub
            "# Quickstart\n",       # quickstart — ok
            "# backend doc\n",      # services/backend
            "# frontend doc\n",     # services/frontend
        ])
        def llm(s, u):
            try:
                return next(responses)
            except StopIteration:
                return "# fallback"
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=NarrativeWriter(llm_invoker=llm),
        )
        result = gen.run()
        # The phase runs to completion.
        readme = next(d for d in result.docs if d.rel_path == "README.md")
        assert readme.succeeded is False
        assert "Auto-generation" in (
            (tmp_path / "docs" / "README.md").read_text(encoding="utf-8")
        )
        # Other docs are fine.
        quickstart = next(d for d in result.docs if d.rel_path == "quickstart.md")
        assert quickstart.succeeded is True

    def test_idempotent_overwrite(self, tmp_path):
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=_fake_writer(),
        )
        gen.run()
        # Run again with different LLM output.
        new_writer = NarrativeWriter(llm_invoker=lambda s, u: "# v2 content")
        gen2 = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=new_writer,
        )
        gen2.run()
        # README contains v2 (overwritten).
        readme = (tmp_path / "docs" / "README.md").read_text(encoding="utf-8")
        assert "v2 content" in readme

    def test_passed_flag_reflects_any_failure(self, tmp_path):
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=NarrativeWriter(
                llm_invoker=lambda s, u: None,   # always fails
            ),
        )
        result = gen.run()
        assert result.passed is False
        # Deterministic docs still succeed.
        det = [d for d in result.docs if d.method == "deterministic"]
        assert all(d.succeeded for d in det)

    def test_status_callback_emits_progress(self, tmp_path):
        statuses: List[str] = []
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=_fake_writer(),
            on_status=lambda m: statuses.append(m),
        )
        gen.run()
        joined = " ".join(statuses)
        assert "architecture" in joined
        assert "infrastructure" in joined
        assert "done" in joined

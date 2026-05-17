"""End-to-end docs-serving roundtrip — item 13 Phase 4 (2026-05-17).

Pins the contract between the producer and the consumer of
``<project>/docs/``:

- **Producer** — ``HumanDocsGenerator`` in this repo writes
  ``architecture.md``, ``api/<svc>.md``, ``services/<svc>.md``,
  ``milestones/m<N>.md``, ``README.md``, ``quickstart.md``,
  ``infrastructure.md``, ``auth.md``, and a ``manifest.json``
  describing the tree.

- **Consumer** — the FastAPI skeleton's ``DocsLoader`` (lives in
  ``~/bizniz-skeleton-fastapi/app/services/docs_loader.py``)
  reads the manifest, builds an in-memory index, and serves
  individual articles by slug.

The two repos can drift independently. This test catches the
class of drift where the producer's slug/section convention stops
matching the consumer's. It runs the loader against a generated
tree and asserts every produced doc is reachable.

Skipped automatically when the skeleton checkout isn't present —
keeps the test useful in CI without forcing skeleton clone everywhere.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.documenters.human_docs.generator import (
    HumanDocsGenerator,
    MilestoneDocInput,
)
from bizniz.documenters.human_docs.llm_narrative import (
    NarrativeResult,
    NarrativeWriter,
)


SKELETON_ROOT = Path.home() / "bizniz-skeleton-fastapi"
LOADER_PATH = SKELETON_ROOT / "app" / "services" / "docs_loader.py"


# ── Test-side stand-in for the skeleton's `app.schemas.docs` ─────


def _install_schema_stubs() -> None:
    """The skeleton's loader imports its DTOs from `app.schemas.docs`.
    The bizniz repo doesn't have that package; stub the module tree
    with pydantic equivalents so the loader file imports cleanly.
    Idempotent — safe to call multiple times across test runs."""
    if "app.schemas.docs" in sys.modules:
        return

    from datetime import datetime
    from typing import List as _List, Optional as _Opt
    from pydantic import BaseModel, Field as PydField

    class DocsTreeNode(BaseModel):
        slug: str
        title: str
        path: str
        section: _Opt[str] = None
        children: _List["DocsTreeNode"] = PydField(default_factory=list)
    DocsTreeNode.model_rebuild()

    class DocsIndexDto(BaseModel):
        version: int = 1
        generated_at: _Opt[datetime] = None
        tree: _List[DocsTreeNode] = PydField(default_factory=list)

    class DocsArticleDto(BaseModel):
        slug: str
        title: str
        path: str
        section: _Opt[str] = None
        markdown: str
        word_count: int = 0

    class DocsSearchHitDto(BaseModel):
        slug: str
        title: str
        excerpt: str
        score: float = 1.0

    app_mod = types.ModuleType("app")
    app_mod.__path__ = []  # mark as package
    sys.modules.setdefault("app", app_mod)
    schemas_mod = types.ModuleType("app.schemas")
    schemas_mod.__path__ = []
    sys.modules.setdefault("app.schemas", schemas_mod)

    docs_mod = types.ModuleType("app.schemas.docs")
    docs_mod.DocsTreeNode = DocsTreeNode
    docs_mod.DocsIndexDto = DocsIndexDto
    docs_mod.DocsArticleDto = DocsArticleDto
    docs_mod.DocsSearchHitDto = DocsSearchHitDto
    sys.modules["app.schemas.docs"] = docs_mod


def _load_docs_loader_module():
    """Import the skeleton's docs_loader.py as a fresh module —
    bypasses the skeleton's package structure so we don't need to
    install it. Returns the loaded module."""
    _install_schema_stubs()
    spec = importlib.util.spec_from_file_location(
        "_skeleton_docs_loader", LOADER_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Fixtures ─────────────────────────────────────────────────────


def _arch() -> SystemArchitecture:
    """Two-service architecture similar to recipe_v2 / property_manager."""
    return SystemArchitecture(
        project_name="Roundtrip App",
        project_slug="roundtrip_app",
        description="A test app for the docs-roundtrip E2E.",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend",
                framework="fastapi", language="python",
                description="API backend", workspace_name="backend",
                port=8000,
            ),
            ServiceDefinition(
                name="frontend", service_type="frontend",
                framework="react", language="typescript",
                description="SPA", workspace_name="frontend", port=5173,
            ),
        ],
    )


def _fake_writer() -> NarrativeWriter:
    """NarrativeWriter that returns canned, distinctive content per
    section so we can verify the roundtrip preserves it."""
    counter = {"i": 0}

    def fake(_sys: str, _user: str) -> str:
        counter["i"] += 1
        return f"# Doc {counter['i']}\n\nGenerated body {counter['i']}.\n"

    return NarrativeWriter(llm_invoker=fake)


@pytest.fixture
def populated_docs_dir(tmp_path):
    """Run HumanDocsGenerator into tmp_path. Returns the docs/ dir."""
    gen = HumanDocsGenerator(
        project_root=tmp_path,
        architecture=_arch(),
        narrative_writer=_fake_writer(),
        compose_yaml="services: {}",
        openapi_per_service={
            "backend": {
                "info": {"title": "Roundtrip API", "version": "0.1"},
                "paths": {
                    "/health": {"get": {"summary": "Health check"}},
                    "/users": {"get": {"summary": "List users"}},
                },
            },
        },
        problem_statement="Build a roundtrip-tested app.",
        milestones=[
            MilestoneDocInput(index=1, name="Auth"),
            MilestoneDocInput(index=2, name="Recipes CRUD"),
        ],
    )
    result = gen.run()
    assert result.passed, f"HumanDocsGenerator failed: {result}"
    return tmp_path / "docs"


# ── Roundtrip tests ──────────────────────────────────────────────


@pytest.mark.skipif(
    not LOADER_PATH.exists(),
    reason=(
        f"fastapi skeleton not present at {SKELETON_ROOT}. "
        f"This roundtrip test requires the sibling skeleton checkout."
    ),
)
class TestDocsRoundtrip:
    """Run the actual DocsLoader against HumanDocsGenerator output."""

    def test_manifest_loads_without_error(self, populated_docs_dir):
        mod = _load_docs_loader_module()
        loader = mod.DocsLoader(populated_docs_dir, auto_reload=False)
        idx = loader.get_index()
        assert idx.version == 1
        assert idx.tree, "expected non-empty tree"

    def test_every_generated_doc_is_reachable_by_slug(
        self, populated_docs_dir,
    ):
        """For every .md the generator wrote, the loader's
        get_article() must return non-None with matching content.

        This is the contract: producer's slug convention === consumer's."""
        mod = _load_docs_loader_module()
        loader = mod.DocsLoader(populated_docs_dir, auto_reload=False)

        expected_slugs: List[str] = []
        for path in sorted(populated_docs_dir.rglob("*.md")):
            rel = path.relative_to(populated_docs_dir)
            slug = str(rel.with_suffix("")).replace("\\", "/")
            expected_slugs.append(slug)

        # These are the docs the generator always writes (assuming
        # the canned NarrativeWriter doesn't fail).
        assert "README" in expected_slugs
        assert "quickstart" in expected_slugs
        assert "architecture" in expected_slugs
        assert "infrastructure" in expected_slugs
        assert "auth" in expected_slugs
        assert "api/backend" in expected_slugs
        assert "services/backend" in expected_slugs
        assert "services/frontend" in expected_slugs
        assert "milestones/m1" in expected_slugs
        assert "milestones/m2" in expected_slugs

        for slug in expected_slugs:
            article = loader.get_article(slug)
            assert article is not None, (
                f"Loader couldn't find slug {slug!r} that generator "
                f"wrote. Producer/consumer contract violation."
            )
            assert article.markdown, f"Empty markdown for {slug!r}"

    def test_manifest_drives_index_sort_order(self, populated_docs_dir):
        """The manifest's `_SECTION_ORDER` puts root (README,
        quickstart) before reference (architecture/infra/auth) before
        api/services/milestones. The loader builds the index from the
        manifest when present; assert that ordering survives."""
        mod = _load_docs_loader_module()
        loader = mod.DocsLoader(populated_docs_dir, auto_reload=False)
        idx = loader.get_index()

        # Flatten the tree to slug list in render order.
        flat: List[str] = []
        for node in idx.tree:
            if node.children:
                # Section node — children are the actual articles.
                for child in node.children:
                    flat.append(child.slug)
            else:
                flat.append(node.slug)

        # README must precede architecture.
        assert flat.index("README") < flat.index("architecture"), (
            f"Order violation — flat slugs: {flat}"
        )
        # architecture (reference) must precede api/backend.
        assert flat.index("architecture") < flat.index("api/backend")
        # api/backend (api) must precede services/backend.
        assert flat.index("api/backend") < flat.index("services/backend")
        # services/backend must precede milestones.
        assert flat.index("services/backend") < flat.index("milestones/m1")

    def test_search_finds_unique_generator_content(
        self, populated_docs_dir,
    ):
        """The canned NarrativeWriter emits "Generated body N" in
        every LLM doc. Search for it; multiple hits should come back."""
        mod = _load_docs_loader_module()
        loader = mod.DocsLoader(populated_docs_dir, auto_reload=False)
        hits = loader.search("Generated body")
        # README + quickstart + per-service + per-milestone = at least 4 LLM docs.
        assert len(hits) >= 4

    def test_path_traversal_still_blocked_on_real_tree(
        self, populated_docs_dir,
    ):
        mod = _load_docs_loader_module()
        loader = mod.DocsLoader(populated_docs_dir, auto_reload=False)
        # Even though there's a real README in this tree, traversal
        # back to it must not work via a malicious slug.
        assert loader.get_article("../docs/README") is None
        assert loader.get_article("/etc/passwd") is None
        assert loader.get_article("api/../../etc/passwd") is None


# ── Contract-shape tests (no skeleton required) ──────────────────


class TestManifestShape:
    """These tests run even without the skeleton checkout — they
    verify the manifest the generator emits has the shape DocsLoader
    expects, by inspection rather than by execution."""

    def test_manifest_has_required_fields(self, tmp_path):
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=_fake_writer(),
        )
        gen.run()
        manifest = json.loads(
            (tmp_path / "docs" / "manifest.json").read_text()
        )
        assert manifest["version"] == 1
        assert "generated_at" in manifest
        assert "tree" in manifest
        assert isinstance(manifest["tree"], list)

        for entry in manifest["tree"]:
            assert "slug" in entry
            assert "title" in entry
            assert "path" in entry
            # Slug shape that DocsLoader._is_safe_slug accepts.
            assert ".." not in entry["slug"]
            assert not entry["slug"].startswith("/")
            assert not entry["slug"].startswith(".")
            # Path ends with .md.
            assert entry["path"].endswith(".md")

    def test_manifest_entries_match_disk(self, tmp_path):
        """Every manifest entry must have a corresponding .md on
        disk — otherwise DocsLoader would refuse to add it to the
        tree (it filters unknown slugs)."""
        gen = HumanDocsGenerator(
            project_root=tmp_path,
            architecture=_arch(),
            narrative_writer=_fake_writer(),
            milestones=[MilestoneDocInput(index=1, name="M1")],
        )
        gen.run()
        manifest = json.loads(
            (tmp_path / "docs" / "manifest.json").read_text()
        )
        docs_dir = tmp_path / "docs"
        for entry in manifest["tree"]:
            md_path = docs_dir / entry["path"]
            assert md_path.exists(), (
                f"Manifest entry {entry['slug']!r} points at "
                f"{entry['path']} but that file does not exist."
            )

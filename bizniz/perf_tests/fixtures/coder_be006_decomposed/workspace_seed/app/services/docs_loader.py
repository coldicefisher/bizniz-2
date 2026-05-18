"""In-memory loader for ``<project>/docs/*.md``.

The Provisioner mounts the project-root ``docs/`` directory into
every Python service container at ``/app/docs`` (read-only). This
loader scans that tree at startup, builds a slug-keyed index, and
re-scans on mtime change.

**Always present in the skeleton.** Engineer-generated code never
edits this file. The viewer at ``/api/v1/docs/*`` is the only
consumer.

If ``<project>/docs/manifest.json`` exists (HumanDocsGenerator
writes it), the tree comes from there — stable ordering and section
grouping. Otherwise the loader auto-derives one from the on-disk
tree (works for hand-managed docs too).
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.schemas.docs import (
    DocsArticleDto,
    DocsIndexDto,
    DocsSearchHitDto,
    DocsTreeNode,
)


# Path traversal guard — slugs must match this. Permits forward
# slashes (nested paths) but no parent-dir escape, no leading slash,
# no Windows reserved chars.
_VALID_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-./]*$")


class DocsLoader:
    """Loads + caches the project's markdown docs from a directory.

    Thread-safe (uses a lock around cache invalidation). Reloads
    automatically when the docs directory's mtime advances — cheap
    enough to check on every request in dev; production deployments
    can disable by setting ``auto_reload=False``.
    """

    def __init__(
        self,
        docs_root: Path,
        *,
        auto_reload: bool = True,
    ) -> None:
        self._docs_root = Path(docs_root)
        self._auto_reload = auto_reload
        self._lock = threading.Lock()
        self._last_scan_mtime: float = 0.0
        # Cache state.
        self._index: DocsIndexDto = DocsIndexDto(tree=[])
        self._articles: Dict[str, DocsArticleDto] = {}
        # Populate eagerly so first request is fast.
        self._reload_locked()

    # ── Public ─────────────────────────────────────────────────────

    def get_index(self) -> DocsIndexDto:
        self._maybe_reload()
        return self._index

    def get_article(self, slug: str) -> Optional[DocsArticleDto]:
        if not self._is_safe_slug(slug):
            return None
        self._maybe_reload()
        return self._articles.get(slug)

    def search(self, query: str, limit: int = 20) -> List[DocsSearchHitDto]:
        """Substring search across cached article bodies. Case-insensitive."""
        if not query or len(query.strip()) < 2:
            return []
        q = query.strip().lower()
        self._maybe_reload()
        hits: List[DocsSearchHitDto] = []
        for slug, article in self._articles.items():
            body = article.markdown.lower()
            idx = body.find(q)
            if idx < 0:
                # Try matching the title too — useful for short queries.
                if q not in article.title.lower():
                    continue
                hits.append(DocsSearchHitDto(
                    slug=slug, title=article.title,
                    excerpt=article.markdown[:200],
                    score=0.5,
                ))
                continue
            start = max(0, idx - 80)
            end = min(len(article.markdown), idx + len(q) + 120)
            excerpt = article.markdown[start:end].replace("\n", " ")
            if start > 0:
                excerpt = "…" + excerpt
            if end < len(article.markdown):
                excerpt = excerpt + "…"
            hits.append(DocsSearchHitDto(
                slug=slug, title=article.title,
                excerpt=excerpt,
                # Crude scoring: earlier in body = higher score.
                score=max(0.1, 1.0 - (idx / max(1, len(body)))),
            ))
            if len(hits) >= limit * 3:
                break
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    # ── Internals ──────────────────────────────────────────────────

    @staticmethod
    def _is_safe_slug(slug: str) -> bool:
        if not slug or len(slug) > 256:
            return False
        if ".." in slug or slug.startswith(("/", ".")):
            return False
        return bool(_VALID_SLUG_RE.match(slug))

    def _maybe_reload(self) -> None:
        if not self._auto_reload:
            return
        try:
            current_mtime = self._docs_root.stat().st_mtime if self._docs_root.exists() else 0.0
        except OSError:
            return
        if current_mtime > self._last_scan_mtime:
            with self._lock:
                # Re-check after acquiring lock — another thread may
                # have just reloaded.
                if current_mtime > self._last_scan_mtime:
                    self._reload_locked()

    def _reload_locked(self) -> None:
        if not self._docs_root.exists():
            self._index = DocsIndexDto(tree=[])
            self._articles = {}
            self._last_scan_mtime = 0.0
            return

        try:
            self._last_scan_mtime = self._docs_root.stat().st_mtime
        except OSError:
            self._last_scan_mtime = 0.0

        # Walk for markdown files first — needed regardless of
        # whether a manifest is present.
        articles, sections = self._scan_filesystem()
        self._articles = articles

        manifest = self._read_manifest()
        if manifest is not None:
            self._index = self._build_index_from_manifest(manifest)
        else:
            self._index = self._build_index_from_filesystem(articles, sections)

    def _scan_filesystem(self) -> Tuple[Dict[str, DocsArticleDto], Dict[str, List[str]]]:
        """Walk ``<docs_root>/**/*.md`` and return (articles, section→slugs).

        The returned articles dict is keyed by slug (relative path
        under docs/, no extension). Hidden files (dotfiles) and the
        manifest itself are excluded.
        """
        articles: Dict[str, DocsArticleDto] = {}
        sections: Dict[str, List[str]] = {}
        for path in sorted(self._docs_root.rglob("*.md")):
            if path.name.startswith("."):
                continue
            rel = path.relative_to(self._docs_root)
            slug = str(rel.with_suffix("")).replace(os.sep, "/")
            if not self._is_safe_slug(slug):
                continue
            try:
                markdown = path.read_text(encoding="utf-8")
            except OSError:
                continue
            title = self._extract_title(markdown) or path.stem.replace("-", " ").replace("_", " ").title()
            parts = slug.split("/")
            section = parts[0] if len(parts) > 1 else None
            articles[slug] = DocsArticleDto(
                slug=slug,
                title=title,
                path=str(rel).replace(os.sep, "/"),
                section=section,
                markdown=markdown,
                word_count=len(markdown.split()),
            )
            if section:
                sections.setdefault(section, []).append(slug)
        return articles, sections

    def _read_manifest(self) -> Optional[dict]:
        manifest_path = self._docs_root / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _build_index_from_manifest(self, manifest: dict) -> DocsIndexDto:
        """Trust the manifest's stated tree shape; fall back to
        on-disk articles when the manifest references unknown slugs."""
        generated_at: Optional[datetime] = None
        gen_raw = manifest.get("generated_at")
        if isinstance(gen_raw, str):
            try:
                generated_at = datetime.fromisoformat(gen_raw.replace("Z", "+00:00"))
            except ValueError:
                generated_at = None

        raw_entries = manifest.get("tree") or []
        tree: List[DocsTreeNode] = []
        # Group flat manifest entries by section, preserving the
        # producer's order — sections appear in the order their first
        # entry appears in the manifest; entries within a section
        # preserve manifest order. HumanDocsGenerator emits the
        # manifest in a deliberate Overview → Reference → API →
        # Services → Milestones order; alphabetizing here would
        # silently throw that work away.
        by_section: Dict[str, List[DocsTreeNode]] = {}
        section_order: List[str] = []
        roots: List[DocsTreeNode] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            slug = entry.get("slug")
            if not slug or not self._is_safe_slug(slug):
                continue
            # Only include slugs that exist on disk; the manifest can
            # drift from reality during partial regeneration.
            if slug not in self._articles:
                continue
            article = self._articles[slug]
            node = DocsTreeNode(
                slug=slug,
                title=entry.get("title") or article.title,
                path=entry.get("path") or article.path,
                section=entry.get("section") or article.section,
            )
            if node.section:
                if node.section not in by_section:
                    by_section[node.section] = []
                    section_order.append(node.section)
                by_section[node.section].append(node)
            else:
                roots.append(node)
        for section in section_order:
            children = by_section[section]
            tree.append(DocsTreeNode(
                slug=section,
                title=section.replace("-", " ").replace("_", " ").title(),
                path=f"{section}/",
                section=section,
                children=children,
            ))
        tree.extend(roots)
        return DocsIndexDto(
            version=int(manifest.get("version") or 1),
            generated_at=generated_at,
            tree=tree,
        )

    def _build_index_from_filesystem(
        self,
        articles: Dict[str, DocsArticleDto],
        sections: Dict[str, List[str]],
    ) -> DocsIndexDto:
        tree: List[DocsTreeNode] = []
        for section, slugs in sorted(sections.items()):
            children = [
                DocsTreeNode(
                    slug=slug,
                    title=articles[slug].title,
                    path=articles[slug].path,
                    section=section,
                )
                for slug in sorted(slugs)
            ]
            tree.append(DocsTreeNode(
                slug=section,
                title=section.replace("-", " ").replace("_", " ").title(),
                path=f"{section}/",
                section=section,
                children=children,
            ))
        # Root-level articles (no section).
        for slug, article in sorted(articles.items()):
            if article.section is None:
                tree.append(DocsTreeNode(
                    slug=slug,
                    title=article.title,
                    path=article.path,
                    section=None,
                ))
        return DocsIndexDto(tree=tree)

    @staticmethod
    def _extract_title(markdown: str) -> Optional[str]:
        """Pull the first H1 from the markdown body, if any."""
        for line in markdown.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
            if stripped and not stripped.startswith("#"):
                # Hit non-empty non-heading line — stop scanning.
                return None
        return None


# ── Singleton accessor ────────────────────────────────────────────


_loader: Optional[DocsLoader] = None
_loader_lock = threading.Lock()


def get_docs_loader() -> DocsLoader:
    """FastAPI dependency — returns the process-wide DocsLoader.

    The mount path ``/app/docs`` is set by the Provisioner-generated
    docker-compose: ``volumes: - ../../docs:/app/docs:ro``. Override
    via ``BIZNIZ_DOCS_ROOT`` for dev/test setups outside docker.
    """
    global _loader
    if _loader is None:
        with _loader_lock:
            if _loader is None:
                root = Path(os.environ.get("BIZNIZ_DOCS_ROOT") or "/app/docs")
                _loader = DocsLoader(root)
    return _loader


def reset_docs_loader_for_testing() -> None:
    """Clear the singleton between tests."""
    global _loader
    with _loader_lock:
        _loader = None

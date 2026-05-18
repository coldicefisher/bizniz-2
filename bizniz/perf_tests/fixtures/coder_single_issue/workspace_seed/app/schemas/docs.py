"""DTOs for the docs-serving routes.

The docs surface is **always present** in this skeleton — every
generated app ships with `/api/v1/docs/*` routes that serve the
markdown HumanDocsGenerator writes to ``<project>/docs/``.

Shape mirrors MUSE's HelpService DTOs (which proved out the pattern
in production). The slug is the relative path under ``docs/``
without the ``.md`` extension; ``api/backend`` → ``docs/api/backend.md``.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class DocsTreeNode(BaseModel):
    """One entry in the docs navigation tree.

    Hierarchy comes from the path — e.g. ``api/backend.md`` lives
    under the ``api`` section. The viewer renders this as a
    sidebar / tree.
    """
    slug: str = Field(..., description="Path under docs/, no extension")
    title: str = Field(..., description="Human-readable title (H1 or filename)")
    path: str = Field(..., description="Relative path under docs/")
    section: Optional[str] = Field(
        default=None,
        description="Top-level grouping (e.g. 'api', 'services', 'milestones')",
    )
    children: List["DocsTreeNode"] = Field(default_factory=list)


DocsTreeNode.model_rebuild()


class DocsIndexDto(BaseModel):
    """The navigation manifest the viewer fetches once at load."""
    version: int = 1
    generated_at: Optional[datetime] = None
    tree: List[DocsTreeNode] = Field(default_factory=list)


class DocsArticleDto(BaseModel):
    """One article's full content."""
    slug: str
    title: str
    path: str
    section: Optional[str] = None
    markdown: str
    word_count: int = 0


class DocsSearchHitDto(BaseModel):
    """One search result."""
    slug: str
    title: str
    excerpt: str = Field(
        ...,
        description="~200 chars of context around the match, with the query term preserved",
    )
    score: float = 1.0

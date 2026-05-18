"""Docs-serving routes — always present.

Every generated app ships with these routes. HumanDocsGenerator
writes markdown to ``<project>/docs/`` after each milestone; this
router serves it via the same auth surface as the rest of the app.

**Ship-with-skeleton — engineer never edits.** If you find yourself
wanting to add docs-related endpoints (e.g. "current user can view
admin docs"), do it in a separate route file that imports
``get_docs_loader`` and ``get_current_user``.

Path-traversal guard: ``DocsLoader._is_safe_slug`` rejects
``..``, leading ``/``, and Windows reserved chars. The FastAPI
``{slug:path}`` matcher accepts forward slashes which is fine for
nested paths like ``api/backend``.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import get_current_user
from app.models.user import User
from app.schemas.docs import DocsArticleDto, DocsIndexDto, DocsSearchHitDto
from app.services.docs_loader import DocsLoader, get_docs_loader

log = logging.getLogger(__name__)
router = APIRouter(prefix="/docs", tags=["docs"])


@router.get("/index", response_model=DocsIndexDto)
async def get_docs_index(
    current_user: User = Depends(get_current_user),
    loader: DocsLoader = Depends(get_docs_loader),
) -> DocsIndexDto:
    """Return the navigation manifest for the docs viewer.

    Loaded once on first call; cached in-memory; auto-reloaded when
    the docs directory's mtime advances.
    """
    return loader.get_index()


@router.get("/article/{slug:path}", response_model=DocsArticleDto)
async def get_docs_article(
    slug: str,
    current_user: User = Depends(get_current_user),
    loader: DocsLoader = Depends(get_docs_loader),
) -> DocsArticleDto:
    """Return one markdown article by slug.

    Slug is the path under ``docs/`` without the ``.md`` extension —
    e.g. ``api/backend`` resolves to ``docs/api/backend.md``.
    """
    article = loader.get_article(slug)
    if article is None:
        # Distinguish "not on disk" from "slug rejected" only in
        # logs; both surface as 404 to clients.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Article not found: {slug!r}",
        )
    return article


@router.get("/search", response_model=list[DocsSearchHitDto])
async def search_docs(
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    loader: DocsLoader = Depends(get_docs_loader),
) -> list[DocsSearchHitDto]:
    """Substring search across cached article bodies."""
    return loader.search(q, limit=limit)

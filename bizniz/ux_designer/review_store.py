"""SQLite-backed cache of UX review results per (project, route).

Stage C of the UX_v2 architecture. The store lets ProUXDesigner skip
routes whose review is still valid on a re-run, and re-run only the
routes touched by recent changes.

Dirty signals:
  - The route's source file mtime is newer than the recorded review
    mtime → that route is dirty.
  - Any watched "global style" file (tailwind.config.{ts,js},
    src/index.css, src/styles/index.css, postcss.config.{js,cjs}) is
    newer than the recorded global-styles mtime → ALL routes are
    dirty (we just changed the foundation).

DB lives at ``<project_root>/.bizniz/ux_reviews.db``. Schema is
created on first open if missing.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


# Files we treat as "global styles" — a change to any of these
# invalidates every route's review. Paths are workspace-relative.
GLOBAL_STYLE_FILES = (
    "tailwind.config.ts",
    "tailwind.config.js",
    "tailwind.config.cjs",
    "src/index.css",
    "src/styles/index.css",
    "src/main.css",
    "postcss.config.js",
    "postcss.config.cjs",
)


class ReviewRecord(BaseModel):
    project_slug: str
    route: str
    view_type: str = ""
    requires_auth: Optional[bool] = None
    last_score: Optional[int] = None
    iterations_to_acceptable: Optional[int] = None
    last_reviewed_at: datetime = Field(default_factory=datetime.utcnow)
    source_file: Optional[str] = None
    source_mtime: Optional[float] = None
    global_styles_mtime: Optional[float] = None


class ReviewStore:
    """SQLite wrapper. Single table, indexed on (project_slug, route)."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS route_reviews (
      project_slug   TEXT NOT NULL,
      route          TEXT NOT NULL,
      view_type      TEXT NOT NULL DEFAULT '',
      requires_auth  INTEGER,  -- 0/1, NULL for unknown
      last_score     INTEGER,
      iterations_to_acceptable INTEGER,
      last_reviewed_at TEXT NOT NULL,
      source_file    TEXT,
      source_mtime   REAL,
      global_styles_mtime REAL,
      PRIMARY KEY (project_slug, route)
    );
    """

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self._SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, project_slug: str, route: str) -> Optional[ReviewRecord]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM route_reviews "
                "WHERE project_slug = ? AND route = ?",
                (project_slug, route),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    def upsert(self, record: ReviewRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO route_reviews
                  (project_slug, route, view_type, requires_auth,
                   last_score, iterations_to_acceptable,
                   last_reviewed_at, source_file, source_mtime,
                   global_styles_mtime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_slug, route) DO UPDATE SET
                  view_type = excluded.view_type,
                  requires_auth = excluded.requires_auth,
                  last_score = excluded.last_score,
                  iterations_to_acceptable = excluded.iterations_to_acceptable,
                  last_reviewed_at = excluded.last_reviewed_at,
                  source_file = excluded.source_file,
                  source_mtime = excluded.source_mtime,
                  global_styles_mtime = excluded.global_styles_mtime
                """,
                (
                    record.project_slug,
                    record.route,
                    record.view_type,
                    int(record.requires_auth) if record.requires_auth is not None else None,
                    record.last_score,
                    record.iterations_to_acceptable,
                    record.last_reviewed_at.isoformat(),
                    record.source_file,
                    record.source_mtime,
                    record.global_styles_mtime,
                ),
            )
            conn.commit()

    def list_for_project(self, project_slug: str) -> List[ReviewRecord]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM route_reviews WHERE project_slug = ? "
                "ORDER BY route",
                (project_slug,),
            )
            return [_row_to_record(r) for r in cur.fetchall()]

    @staticmethod
    def is_dirty(
        record: ReviewRecord,
        *,
        current_source_mtime: Optional[float],
        current_globals_mtime: Optional[float],
        acceptable_score: int,
    ) -> tuple:
        """Decide whether to re-review the route. Returns
        ``(dirty: bool, reason: str)``.

        Triggers (any one fires dirty):
          1. Last score < acceptable threshold → always re-review,
             we never converged.
          2. Source file mtime newer than recorded → the route's
             code changed.
          3. Global styles mtime newer than recorded → the design
             foundation moved, every route gets re-reviewed.
        """
        if record.last_score is None or record.last_score < acceptable_score:
            return (
                True,
                f"prior score {record.last_score} below threshold "
                f"{acceptable_score}",
            )
        if (
            current_source_mtime is not None
            and record.source_mtime is not None
            and current_source_mtime > record.source_mtime + 0.001
        ):
            return (True, "route source file changed since last review")
        if (
            current_globals_mtime is not None
            and record.global_styles_mtime is not None
            and current_globals_mtime > record.global_styles_mtime + 0.001
        ):
            return (True, "global style file changed since last review")
        return (False, "")


def max_global_mtime(workspace_root: Path) -> Optional[float]:
    """Return the latest mtime across the global-style watch list, or
    None if no watched file exists."""
    best: Optional[float] = None
    for rel in GLOBAL_STYLE_FILES:
        fp = workspace_root / rel
        if not fp.exists():
            continue
        try:
            m = fp.stat().st_mtime
        except OSError:
            continue
        if best is None or m > best:
            best = m
    return best


def source_mtime(workspace_root: Path, source_file_rel: Optional[str]) -> Optional[float]:
    """Mtime of the route's source file, or None if path missing /
    file gone."""
    if not source_file_rel:
        return None
    fp = workspace_root / source_file_rel
    if not fp.exists():
        return None
    try:
        return fp.stat().st_mtime
    except OSError:
        return None


def _row_to_record(row: sqlite3.Row) -> ReviewRecord:
    return ReviewRecord(
        project_slug=row["project_slug"],
        route=row["route"],
        view_type=row["view_type"] or "",
        requires_auth=(
            bool(row["requires_auth"]) if row["requires_auth"] is not None else None
        ),
        last_score=row["last_score"],
        iterations_to_acceptable=row["iterations_to_acceptable"],
        last_reviewed_at=datetime.fromisoformat(row["last_reviewed_at"]),
        source_file=row["source_file"],
        source_mtime=row["source_mtime"],
        global_styles_mtime=row["global_styles_mtime"],
    )

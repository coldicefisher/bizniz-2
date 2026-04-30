"""
Project skeletons for service seeding.

Skeletons are pre-built starter repos cloned to local disk that the architect
can use to seed services with batteries-included starting points (auth, Docker,
tests, README, etc.) instead of generating everything from scratch.

Skeletons live under ``$BIZNIZ_SKELETONS_DIR`` (default: ``~/``):

    ~/bizniz-skeleton-fastapi/      → fastapi
    ~/bizniz-skeleton-react/        → react
    ~/bizniz-skeleton-angular/      → angular
    ~/bizniz-skeleton-teams/        → teams-backend, teams-consumer, teams-frontend

The architect picks ``skeleton: fastapi | react | angular | teams-backend |
teams-consumer | teams-frontend | none`` per service in its decomposition step.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class SkeletonInfo:
    name: str
    relative_path: str
    service_type: str
    framework: str
    language: str
    container_port: Optional[int]
    description: str


_SKELETONS: Dict[str, SkeletonInfo] = {
    "fastapi": SkeletonInfo(
        name="fastapi",
        relative_path="bizniz-skeleton-fastapi",
        service_type="backend",
        framework="fastapi",
        language="python",
        container_port=8000,
        description=(
            "FastAPI backend with full auth (login, refresh, email verification, "
            "password reset, role-checking), Docker, pytest unit + functional "
            "tests, structured logging."
        ),
    ),
    "react": SkeletonInfo(
        name="react",
        relative_path="bizniz-skeleton-react",
        service_type="frontend",
        framework="react",
        language="typescript",
        container_port=5173,
        description=(
            "React + TypeScript + Vite frontend with auth flow (signup/login), "
            "routing, jest tests, Docker. Default for general frontends."
        ),
    ),
    "angular": SkeletonInfo(
        name="angular",
        relative_path="bizniz-skeleton-angular",
        service_type="frontend",
        framework="angular",
        language="typescript",
        container_port=4200,
        description=(
            "Angular frontend with Material Design, NgRx state management, "
            "theming, jasmine/karma tests, Docker. Use for dashboard-heavy / "
            "data-dense UIs."
        ),
    ),
    "teams-backend": SkeletonInfo(
        name="teams-backend",
        relative_path="bizniz-skeleton-teams/backend",
        service_type="backend",
        framework="fastapi",
        language="python",
        container_port=8000,
        description=(
            "Realtime fan-out feed backend (FastAPI + producer). Use as part of "
            "the teams system pattern for Microsoft Teams-like architectures."
        ),
    ),
    "teams-consumer": SkeletonInfo(
        name="teams-consumer",
        relative_path="bizniz-skeleton-teams/consumer",
        service_type="worker",
        framework="celery",
        language="python",
        container_port=None,
        description=(
            "Realtime fan-out feed consumer/worker. Use as part of the teams "
            "system pattern."
        ),
    ),
    "teams-frontend": SkeletonInfo(
        name="teams-frontend",
        relative_path="bizniz-skeleton-teams/frontend-angular",
        service_type="frontend",
        framework="angular",
        language="typescript",
        container_port=4200,
        description=(
            "Angular frontend wired for realtime fan-out feeds. Use as part of "
            "the teams system pattern."
        ),
    ),
}

_EXCLUDE_NAMES = {
    ".git", ".github", "node_modules", "__pycache__", ".pytest_cache",
    "dist", "build", "package-lock.json", ".env",
}


def list_skeletons() -> List[SkeletonInfo]:
    return list(_SKELETONS.values())


def get_skeleton(name: Optional[str]) -> Optional[SkeletonInfo]:
    if not name or name == "none":
        return None
    return _SKELETONS.get(name)


def skeletons_root() -> Path:
    return Path(os.environ.get("BIZNIZ_SKELETONS_DIR", str(Path.home())))


def skeleton_source_path(skeleton: SkeletonInfo) -> Path:
    return skeletons_root() / skeleton.relative_path


def seed_workspace(
    skeleton_name: str,
    dest: Path,
    project_slug: str,
    service_name: str,
) -> List[str]:
    """
    Copy the chosen skeleton into ``dest``, substituting ``{project_slug}``
    and ``{service_name}`` placeholders in text files.

    Skips ``.git``, ``node_modules``, lockfiles, ``.env`` (use ``.env.example``
    instead). Returns the list of relative paths copied.
    """
    skeleton = get_skeleton(skeleton_name)
    if skeleton is None:
        return []

    src = skeleton_source_path(skeleton)
    if not src.exists():
        raise FileNotFoundError(
            f"Skeleton '{skeleton_name}' not found at {src}. "
            f"Set BIZNIZ_SKELETONS_DIR or clone it: "
            f"git clone https://github.com/coldicefisher/{skeleton.relative_path.split('/')[0]}.git "
            f"{src}"
        )

    dest.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []

    for src_path in src.rglob("*"):
        rel = src_path.relative_to(src)
        if any(part in _EXCLUDE_NAMES for part in rel.parts):
            continue
        dst_path = dest / rel
        if src_path.is_dir():
            dst_path.mkdir(parents=True, exist_ok=True)
            continue
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        raw = src_path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            dst_path.write_bytes(raw)
            copied.append(str(rel))
            continue
        text = text.replace("{project_slug}", project_slug)
        text = text.replace("{service_name}", service_name)
        dst_path.write_text(text)
        copied.append(str(rel))

    return copied


def skeletons_summary_for_prompt() -> str:
    """Human-readable description block for the architect prompt."""
    lines = []
    for s in list_skeletons():
        port = f", exposes :{s.container_port}" if s.container_port else ""
        lines.append(
            f"- {s.name} ({s.framework}/{s.language}, {s.service_type}{port}): "
            f"{s.description}"
        )
    lines.append("- none: generate this service from scratch (no skeleton).")
    return "\n".join(lines)

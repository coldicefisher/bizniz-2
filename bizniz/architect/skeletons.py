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
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional


_GITHUB_ORG = "coldicefisher"
_CLONE_TIMEOUT_SECONDS = 120


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
    "saas-api": SkeletonInfo(
        name="saas-api",
        relative_path="bizniz-skeleton-saas/api",
        service_type="backend",
        framework="fastapi",
        language="python",
        container_port=8000,
        description=(
            "Production-shaped FastAPI backend integrated with FusionAuth "
            "(JWT validation, OAuth2 callback, refresh, /me, profile auto-create), "
            "shared core/ package, demo Article entity + long-running regenerate "
            "job dispatched via Redis Streams. Pair with saas-ws + saas-consumer "
            "+ saas-frontend for the full bundle."
        ),
    ),
    "saas-ws": SkeletonInfo(
        name="saas-ws",
        relative_path="bizniz-skeleton-saas/websocket-server",
        service_type="worker",
        framework="fastapi",
        language="python",
        container_port=8001,
        description=(
            "Dedicated WebSocket server. Validates FusionAuth JWT on connect, "
            "subscribes to ws:user:*, ws:room:*, ws:broadcast Redis channels, "
            "routes events to connected sockets. Handles room join/leave for "
            "entity-scoped subscriptions. Part of the saas bundle."
        ),
    ),
    "saas-consumer": SkeletonInfo(
        name="saas-consumer",
        relative_path="bizniz-skeleton-saas/store-consumer",
        service_type="worker",
        framework="redis-streams",
        language="python",
        container_port=None,
        description=(
            "Redis Streams worker for the saas bundle. Registers job handlers, "
            "acquires processing locks (with WS broadcast), runs long-running "
            "tasks, releases locks. Demo job: regenerate_article. Part of the "
            "saas bundle."
        ),
    ),
    "saas-frontend": SkeletonInfo(
        name="saas-frontend",
        relative_path="bizniz-skeleton-saas/frontend",
        service_type="frontend",
        framework="angular",
        language="typescript",
        container_port=5173,
        description=(
            "Angular SPA for the saas bundle. FusionAuth OAuth2 login flow, "
            "JWT in Authorization header (interceptor), protected routes, "
            "EntityChannelService for real-time entity events (mirrors MUSE "
            "pattern), profile editor, article view with live processing-lock "
            "indicator. Part of the saas bundle."
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


def _skeleton_repo_name(skeleton: SkeletonInfo) -> str:
    """The git repo name — first component of relative_path.

    fastapi → bizniz-skeleton-fastapi
    teams-backend → bizniz-skeleton-teams (shared by all teams-* skeletons)
    """
    return skeleton.relative_path.split("/")[0]


def _skeleton_repo_root(skeleton: SkeletonInfo) -> Path:
    return skeletons_root() / _skeleton_repo_name(skeleton)


def _skeleton_clone_url(skeleton: SkeletonInfo) -> str:
    return f"git@github.com:{_GITHUB_ORG}/{_skeleton_repo_name(skeleton)}.git"


def _ensure_skeleton_present(
    skeleton: SkeletonInfo,
    on_status: Optional[Callable[[str], None]] = None,
) -> None:
    """If the skeleton's repo dir is missing, attempt to clone it.

    Multiple skeletons may share a single repo (e.g. teams-backend +
    teams-consumer + teams-frontend all live in bizniz-skeleton-teams).
    We clone the repo root, not the per-skeleton subpath.

    Raises FileNotFoundError on failure, with manual-clone instructions —
    callers (Provisioner) catch this and fall back to generation.
    """
    src = skeleton_source_path(skeleton)
    if src.exists():
        return

    repo_root = _skeleton_repo_root(skeleton)
    repo_name = _skeleton_repo_name(skeleton)
    url = _skeleton_clone_url(skeleton)

    if repo_root.exists():
        # Repo dir exists but the per-skeleton subpath doesn't — that means
        # the repo is there but malformed (wrong layout / partial clone).
        # Don't try to clone over it; surface the problem.
        raise FileNotFoundError(
            f"Skeleton '{skeleton.name}' subpath missing at {src}, but "
            f"{repo_root} exists. Repo layout may be wrong — inspect or "
            f"re-clone: rm -rf {repo_root} && git clone {url} {repo_root}"
        )

    repo_root.parent.mkdir(parents=True, exist_ok=True)
    if on_status:
        on_status(f"Skeleton '{skeleton.name}' missing at {src}; cloning {url} → {repo_root}")
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(repo_root)],
            check=True,
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as e:
        # Clean up partial clone so a retry isn't blocked by repo_root.exists().
        if repo_root.exists():
            shutil.rmtree(repo_root, ignore_errors=True)
        raise FileNotFoundError(
            f"Skeleton '{skeleton.name}' auto-clone failed: "
            f"`git clone {url}` exited {e.returncode}. "
            f"Stderr: {e.stderr.strip() or '(empty)'}. "
            f"Clone manually: git clone {url} {repo_root}"
        ) from e
    except subprocess.TimeoutExpired as e:
        if repo_root.exists():
            shutil.rmtree(repo_root, ignore_errors=True)
        raise FileNotFoundError(
            f"Skeleton '{skeleton.name}' auto-clone timed out after "
            f"{_CLONE_TIMEOUT_SECONDS}s. Clone manually: git clone {url} {repo_root}"
        ) from e
    except FileNotFoundError as e:
        # `git` binary missing.
        raise FileNotFoundError(
            f"Skeleton '{skeleton.name}' auto-clone failed: git not found in PATH. "
            f"Install git or clone manually: git clone {url} {repo_root}"
        ) from e

    if not src.exists():
        # Clone succeeded but the expected subpath isn't where we thought.
        raise FileNotFoundError(
            f"Skeleton '{skeleton.name}' cloned to {repo_root} but expected "
            f"subpath {skeleton.relative_path} is missing. Repo layout may "
            f"have changed."
        )

    if on_status:
        on_status(f"Skeleton '{skeleton.name}' cloned successfully")


def seed_workspace(
    skeleton_name: str,
    dest: Path,
    project_slug: str,
    service_name: str,
    on_status: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """
    Copy the chosen skeleton into ``dest``, substituting ``{project_slug}``
    and ``{service_name}`` placeholders in text files.

    If the skeleton's repo isn't present locally, attempt to clone it from
    ``github.com/coldicefisher/<repo>``. Pass ``on_status`` to surface clone
    progress to a logger.

    Skips ``.git``, ``node_modules``, lockfiles, ``.env`` (use ``.env.example``
    instead). Returns the list of relative paths copied.
    """
    skeleton = get_skeleton(skeleton_name)
    if skeleton is None:
        return []

    _ensure_skeleton_present(skeleton, on_status=on_status)

    src = skeleton_source_path(skeleton)
    if not src.exists():
        raise FileNotFoundError(
            f"Skeleton '{skeleton_name}' not found at {src} after clone attempt."
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

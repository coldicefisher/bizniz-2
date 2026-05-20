"""Deterministic container rebuild after an agent edits dep manifests.

When the agent (or per-issue/per-milestone debugger) modifies
requirements.txt / package.json / Dockerfile inside a service
workspace, the new dependencies aren't visible to the running
container until the container is rebuilt. This module provides
that deterministic rebuild flow.

User contract (per agent prompt):
- Agent writes the dep change. Orchestrator runs this util.
- If util succeeds: agent gets clean re-validation.
- If util fails (install / build error): finding surfaces with
  the error tail for the agent to fix on the next iter.

Two rebuild modes:
- **soft** (cheap, ~10-40s): `pip install -r requirements.txt` or
  `npm install` + restart service. Used when only the manifest
  changed.
- **hard** (expensive, ~1-5 min): `docker compose build` + recreate.
  Used when the Dockerfile itself changed.

Trigger detection: hash the relevant files before/after the agent
runs; if any hash differs, trigger the matching rebuild.
"""
from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, Field


# Files whose change triggers a soft (install + restart) rebuild.
_SOFT_TRIGGERS = ["requirements.txt", "pyproject.toml", "package.json"]
# Files whose change triggers a hard (build + recreate) rebuild.
_HARD_TRIGGERS = ["Dockerfile"]


class RebuildResult(BaseModel):
    """Outcome of a single rebuild attempt."""

    triggered: bool = Field(
        ...,
        description="True if at least one trigger file changed.",
    )
    mode: str = Field(
        default="none",
        description="'soft' | 'hard' | 'none' (no rebuild needed).",
    )
    files_changed: List[str] = Field(default_factory=list)
    success: bool = Field(default=True)
    error_tail: str = Field(
        default="",
        description="Last ~2KB of stderr/stdout when success=False.",
    )
    wall_s: float = Field(default=0.0)


def hash_trigger_files(
    workspace_root: Path,
    triggers: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Build a {filename: sha256-hex} map for trigger files in the
    workspace. Missing files become empty-string entries (so
    additions are detected by the empty→hash transition)."""
    triggers = triggers or (_SOFT_TRIGGERS + _HARD_TRIGGERS)
    out: Dict[str, str] = {}
    for fname in triggers:
        p = workspace_root / fname
        if p.exists() and p.is_file():
            try:
                content = p.read_bytes()
                out[fname] = hashlib.sha256(content).hexdigest()
            except Exception:
                out[fname] = "ERR"
        else:
            out[fname] = ""
    return out


def detect_changes(
    before: Dict[str, str], after: Dict[str, str],
) -> List[str]:
    """Return list of filenames whose hash differs between snapshots."""
    keys = set(before.keys()) | set(after.keys())
    return sorted(k for k in keys if before.get(k, "") != after.get(k, ""))


def maybe_rebuild(
    *,
    compose_path: Optional[str],
    service_name: Optional[str],
    workspace_root: Path,
    before_hashes: Dict[str, str],
    after_hashes: Optional[Dict[str, str]] = None,
    on_status: Optional[Callable[[str], None]] = None,
    health_timeout_s: float = 30.0,
    install_timeout_s: float = 300.0,
    build_timeout_s: float = 600.0,
) -> RebuildResult:
    """If any trigger file changed, rebuild the container.

    - Hard rebuild when Dockerfile changed (slow path).
    - Soft rebuild when only requirements/package.json changed.
    - No-op when no trigger changed OR compose_path/service_name missing.

    Returns RebuildResult. On failure, error_tail carries the last
    ~2KB of subprocess output so the caller can surface as a finding.
    """
    if not compose_path or not service_name:
        # No container infra wired — no-op (used by perf-test sandboxes).
        return RebuildResult(triggered=False, mode="none")

    if after_hashes is None:
        after_hashes = hash_trigger_files(workspace_root)

    changed = detect_changes(before_hashes, after_hashes)
    if not changed:
        return RebuildResult(triggered=False, mode="none")

    hard = any(f in _HARD_TRIGGERS for f in changed)
    mode = "hard" if hard else "soft"

    def _log(msg: str) -> None:
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    _log(
        f"container_rebuild[{service_name}]: {mode} rebuild triggered "
        f"by changes to {changed}"
    )
    t0 = time.time()
    try:
        if hard:
            return _hard_rebuild(
                compose_path=compose_path,
                service_name=service_name,
                changed=changed,
                on_status=on_status,
                build_timeout_s=build_timeout_s,
                health_timeout_s=health_timeout_s,
                t0=t0,
            )
        return _soft_rebuild(
            compose_path=compose_path,
            service_name=service_name,
            changed=changed,
            workspace_root=workspace_root,
            on_status=on_status,
            install_timeout_s=install_timeout_s,
            health_timeout_s=health_timeout_s,
            t0=t0,
        )
    except Exception as e:
        return RebuildResult(
            triggered=True, mode=mode, files_changed=changed,
            success=False,
            error_tail=f"{type(e).__name__}: {e}",
            wall_s=time.time() - t0,
        )


# ── Soft rebuild ──────────────────────────────────────────────────


def _soft_rebuild(
    *,
    compose_path: str,
    service_name: str,
    changed: List[str],
    workspace_root: Path,
    on_status: Optional[Callable[[str], None]],
    install_timeout_s: float,
    health_timeout_s: float,
    t0: float,
) -> RebuildResult:
    """pip install (or npm install) + restart container + health check."""
    # Pick install command based on what changed.
    install_cmd: Optional[List[str]] = None
    if "requirements.txt" in changed or "pyproject.toml" in changed:
        install_cmd = [
            "docker", "compose", "-f", compose_path, "exec", "-T",
            service_name, "pip", "install", "-r", "requirements.txt",
            "--quiet",
        ]
    elif "package.json" in changed:
        install_cmd = [
            "docker", "compose", "-f", compose_path, "exec", "-T",
            service_name, "npm", "install",
        ]
    if install_cmd is None:
        # No matching install — soft rebuild not applicable.
        return RebuildResult(
            triggered=True, mode="soft", files_changed=changed,
            success=True, wall_s=time.time() - t0,
        )

    # Run install.
    try:
        proc = subprocess.run(
            install_cmd,
            capture_output=True, text=True,
            timeout=install_timeout_s,
        )
    except subprocess.TimeoutExpired:
        return RebuildResult(
            triggered=True, mode="soft", files_changed=changed,
            success=False,
            error_tail=f"install timed out after {install_timeout_s}s",
            wall_s=time.time() - t0,
        )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-2000:]
        return RebuildResult(
            triggered=True, mode="soft", files_changed=changed,
            success=False,
            error_tail=f"install exited {proc.returncode}:\n{tail}",
            wall_s=time.time() - t0,
        )

    # Restart the service (so e.g. uvicorn picks up new deps).
    try:
        subprocess.run(
            ["docker", "compose", "-f", compose_path, "restart", service_name],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        pass  # health check below will catch this.

    # Health check.
    health_ok = _wait_for_health(
        compose_path=compose_path,
        service_name=service_name,
        timeout_s=health_timeout_s,
    )
    if not health_ok:
        return RebuildResult(
            triggered=True, mode="soft", files_changed=changed,
            success=False,
            error_tail=(
                f"service {service_name} did not return /health green "
                f"within {health_timeout_s}s after install + restart"
            ),
            wall_s=time.time() - t0,
        )

    return RebuildResult(
        triggered=True, mode="soft", files_changed=changed,
        success=True, wall_s=time.time() - t0,
    )


# ── Hard rebuild ──────────────────────────────────────────────────


def _hard_rebuild(
    *,
    compose_path: str,
    service_name: str,
    changed: List[str],
    on_status: Optional[Callable[[str], None]],
    build_timeout_s: float,
    health_timeout_s: float,
    t0: float,
) -> RebuildResult:
    """docker compose build + up -d + health check."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", compose_path, "build", service_name],
            capture_output=True, text=True,
            timeout=build_timeout_s,
        )
    except subprocess.TimeoutExpired:
        return RebuildResult(
            triggered=True, mode="hard", files_changed=changed,
            success=False,
            error_tail=f"docker build timed out after {build_timeout_s}s",
            wall_s=time.time() - t0,
        )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-2000:]
        return RebuildResult(
            triggered=True, mode="hard", files_changed=changed,
            success=False,
            error_tail=f"docker build exited {proc.returncode}:\n{tail}",
            wall_s=time.time() - t0,
        )
    # Recreate the container.
    try:
        subprocess.run(
            ["docker", "compose", "-f", compose_path, "up", "-d",
             "--force-recreate", service_name],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        pass
    health_ok = _wait_for_health(
        compose_path=compose_path,
        service_name=service_name,
        timeout_s=health_timeout_s,
    )
    return RebuildResult(
        triggered=True, mode="hard", files_changed=changed,
        success=health_ok,
        error_tail=(
            "" if health_ok else
            f"service {service_name} not healthy within {health_timeout_s}s"
        ),
        wall_s=time.time() - t0,
    )


# ── Health check ──────────────────────────────────────────────────


def _wait_for_health(
    *,
    compose_path: str,
    service_name: str,
    timeout_s: float,
    poll_interval_s: float = 1.0,
) -> bool:
    """Poll the service's container until /health returns 2xx, or
    timeout. Returns True on green, False on timeout.

    Strategy: use ``docker compose exec`` to curl /health from inside
    the container (avoids host-port flakiness). If curl isn't
    installed in the image, fall back to ``docker compose ps``
    container-state probe (healthy/running).
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = subprocess.run(
                ["docker", "compose", "-f", compose_path, "exec", "-T",
                 service_name, "curl", "-sf", "http://localhost:8000/health"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
        # Fallback: ps probe.
        try:
            r2 = subprocess.run(
                ["docker", "compose", "-f", compose_path, "ps",
                 "--format", "json", service_name],
                capture_output=True, text=True, timeout=5,
            )
            if r2.returncode == 0 and "running" in (r2.stdout or "").lower():
                # Container's up; assume healthy if no /health endpoint.
                # First success returns; otherwise loop continues.
                pass
        except Exception:
            pass
        time.sleep(poll_interval_s)
    return False

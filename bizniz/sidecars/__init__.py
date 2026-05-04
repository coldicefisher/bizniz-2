"""Sidecar preflight.

The pipeline depends on a fixed set of pre-built Docker images
(documenters, validators, test runners). If any are missing, the
architect/engineer/integration phases fall back to runtime
auto-build — but that's fine for one image, awful when missing
five at once on a fresh machine.

This module gates ALL pipeline work on every required image
existing (or being built upfront). The architect calls
``ensure_sidecars_built()`` once at the top of ``build_with_plan``;
if any image can't be built, the milestone aborts before any AI
calls or provisioner work.

Adding a new sidecar = one entry in ``REQUIRED_SIDECARS``.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class SidecarSpec:
    """A pre-built Docker image the pipeline depends on."""
    image: str               # e.g. "bizniz-doc-typescript:latest"
    dockerfile: Path         # absolute path to the Dockerfile
    context: Path            # absolute path to the docker build context
    purpose: str             # human-readable, surfaced in logs
    timeout_s: int = 600     # build cap


REQUIRED_SIDECARS: List[SidecarSpec] = [
    SidecarSpec(
        image="bizniz-doc-typescript:latest",
        dockerfile=_REPO_ROOT / "docker" / "doc-sidecars" / "Dockerfile.typescript",
        context=_REPO_ROOT / "docker" / "doc-sidecars",
        purpose="TypeScript documenter + tsc validator",
    ),
    SidecarSpec(
        image="bizniz-doc-python:latest",
        dockerfile=_REPO_ROOT / "docker" / "doc-sidecars" / "Dockerfile.python",
        context=_REPO_ROOT / "docker" / "doc-sidecars",
        purpose="Python documenter + mypy validator",
    ),
    SidecarSpec(
        image="bizniz-test-pytest:latest",
        dockerfile=_REPO_ROOT / "docker" / "test-sidecars" / "Dockerfile.pytest",
        context=_REPO_ROOT / "docker" / "test-sidecars",
        purpose="Pytest integration-test runner",
    ),
    SidecarSpec(
        image="bizniz-test-playwright:latest",
        dockerfile=_REPO_ROOT / "docker" / "test-sidecars" / "Dockerfile.playwright",
        context=_REPO_ROOT / "docker" / "test-sidecars",
        purpose="Playwright UI-test runner",
    ),
]


class SidecarPreflightError(RuntimeError):
    """Raised when a required sidecar image cannot be built.

    The architect catches this at the top of build_with_plan and
    aborts the milestone — no AI calls or provisioner work happen
    until the infrastructure is ready.
    """


def image_exists(image: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def build_sidecar(spec: SidecarSpec, on_status: Optional[Callable[[str], None]] = None) -> None:
    if on_status:
        on_status(f"Sidecar preflight: building {spec.image} ({spec.purpose})...")
    if not spec.dockerfile.exists():
        raise SidecarPreflightError(
            f"Dockerfile missing for {spec.image}: {spec.dockerfile}. "
            f"Cannot auto-build."
        )
    proc = subprocess.run(
        ["docker", "build", "-t", spec.image, "-f", str(spec.dockerfile), str(spec.context)],
        capture_output=True, text=True, timeout=spec.timeout_s,
    )
    if proc.returncode != 0:
        raise SidecarPreflightError(
            f"Sidecar build failed for {spec.image}:\n"
            f"{proc.stderr.strip()[:3000]}"
        )
    if on_status:
        on_status(f"Sidecar preflight: {spec.image} ready")


def ensure_sidecars_built(on_status: Optional[Callable[[str], None]] = None) -> None:
    """Verify every required sidecar image exists; build any that
    don't. Aborts the entire pipeline (raises SidecarPreflightError)
    if any can't be built.

    Call once at the top of build_with_plan. After this returns,
    the rest of the pipeline can assume all sidecar images are
    available.
    """
    if not docker_available():
        raise SidecarPreflightError(
            "Docker daemon is not reachable. Sidecar preflight requires "
            "docker to be running."
        )

    missing = [s for s in REQUIRED_SIDECARS if not image_exists(s.image)]
    if not missing:
        if on_status:
            on_status(
                f"Sidecar preflight: all {len(REQUIRED_SIDECARS)} image(s) "
                f"already built — proceeding"
            )
        return

    if on_status:
        on_status(
            f"Sidecar preflight: {len(missing)} of "
            f"{len(REQUIRED_SIDECARS)} image(s) missing — building..."
        )
    for spec in missing:
        build_sidecar(spec, on_status=on_status)

    if on_status:
        on_status(f"Sidecar preflight: all images ready, proceeding")

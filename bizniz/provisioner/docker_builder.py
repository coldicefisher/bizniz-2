"""Docker image build helpers used by the Provisioner."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable, Optional


def build_image(
    image_tag: str,
    context: Path,
    dockerfile: Path,
    log: Optional[Callable[[str], None]] = None,
    timeout: int = 300,
) -> None:
    """Build a Docker image. Raises RuntimeError on failure."""
    def _log(msg: str) -> None:
        if log:
            log(msg)

    if not dockerfile.exists():
        raise FileNotFoundError(f"Dockerfile not found at {dockerfile}")

    _log(f"Provisioner: docker build {image_tag} (from {dockerfile})...")
    t0 = time.time()
    proc = subprocess.run(
        ["docker", "build", "-t", image_tag, "-f", str(dockerfile), str(context)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = time.time() - t0
    if proc.returncode != 0:
        _log(f"Provisioner: docker build FAILED in {elapsed:.1f}s")
        stderr = proc.stderr[:500] if proc.stderr else "(no stderr)"
        _log(f"Provisioner: build error: {stderr}")
        raise RuntimeError(f"Docker build failed: {proc.stderr[:500]}")
    _log(f"Provisioner: docker build OK in {elapsed:.1f}s")

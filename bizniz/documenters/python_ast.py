"""Python documenter — thin orchestrator that dispatches the
``bizniz-doc-python`` sidecar. The actual extraction logic lives
in ``docker/doc-sidecars/extract_python.py`` running inside the
sidecar with a pinned, predictable Python + dependency stack.

Why a sidecar instead of running stdlib ``ast`` in-process:

  1. Symmetry with TypeScript. Both languages run their extractor
     in a language-specific sidecar with their native AST tooling.
     Adding C# / Go / Rust later follows the same shape — new
     sidecar + same dispatcher.
  2. Predictable environment. The sidecar pins Python + key deps
     (pydantic, sqlalchemy, fastapi) so extraction doesn't drift
     across host venvs.
  3. The same image hosts the validator (mypy) — one image does
     extraction AND post-flight type-checking, dispatched by
     overriding the entrypoint.

Costs ~1-2 seconds of docker overhead per call — negligible
against the AI-call latency of the engineer / coder loop.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


SIDECAR_IMAGE = "bizniz-doc-python:latest"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCKERFILE_DIR = _REPO_ROOT / "docker" / "doc-sidecars"


@dataclass
class PythonAstDocumenter:
    """Dispatch the Python documenter sidecar against a service workspace.

    Parameters
    ----------
    workspace_root:
        Service source-code root (e.g. ``~/bizniz_projects/<slug>/backend``).
    service_name:
        Logical name of the service. Embedded in the JSON output.
    timeout_s:
        Cap on the docker run. Real-world extracts run in seconds;
        60 leaves plenty of headroom.
    """

    workspace_root: Path
    service_name: str = ""
    timeout_s: int = 60

    def extract(self) -> Dict[str, Any]:
        self._ensure_image()
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{self.workspace_root}:/workspace:ro",
            SIDECAR_IMAGE,
            "python", "/opt/extractor/extract_python.py",
            "/workspace", self.service_name,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise DocumenterError(
                f"Python documenter timed out after {self.timeout_s}s for "
                f"{self.workspace_root}: {e.stderr or ''}"
            )

        if proc.returncode != 0:
            raise DocumenterError(
                f"Python documenter failed (rc={proc.returncode}) for "
                f"{self.workspace_root}.\nstderr:\n{proc.stderr.strip()[:2000]}"
            )

        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise DocumenterError(
                f"Python documenter produced invalid JSON: {e}\n"
                f"First 500 bytes of stdout:\n{proc.stdout[:500]}"
            )

    def write(self, output_dir: Path) -> Path:
        doc = self.extract()
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "api.json"
        out_path.write_text(json.dumps(doc, indent=2, sort_keys=True))
        return out_path

    # ── image management ────────────────────────────────────────────

    @staticmethod
    def _image_exists(image: str) -> bool:
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _ensure_image(self) -> None:
        if self._image_exists(SIDECAR_IMAGE):
            return
        dockerfile = _DOCKERFILE_DIR / "Dockerfile.python"
        if not dockerfile.exists():
            raise DocumenterError(
                f"Cannot auto-build Python documenter image: "
                f"{dockerfile} not found. Run "
                f"docker/doc-sidecars/build.sh manually."
            )
        proc = subprocess.run(
            ["docker", "build", "-t", SIDECAR_IMAGE,
             "-f", str(dockerfile), str(_DOCKERFILE_DIR)],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            raise DocumenterError(
                f"Auto-build of {SIDECAR_IMAGE} failed:\n"
                f"{proc.stderr.strip()[:2000]}"
            )


class DocumenterError(RuntimeError):
    """Raised when the documenter sidecar fails or produces unparseable output."""

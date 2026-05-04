"""TypeScript documenter — thin Python orchestrator that dispatches
the ``bizniz-doc-typescript`` sidecar against a workspace and parses
the JSON it emits to stdout.

The actual extraction lives in ``docker/doc-sidecars/extract.js`` and
uses ts-morph (the TypeScript compiler API). We launch a sidecar
because:

  1. Real TS parsing requires the TypeScript compiler. Wrapping
     that compiler from Python via regex would be fragile and
     wrong — same anti-pattern as inventing our own HTTP parser
     when ``urllib`` exists.
  2. Same architecture as our pytest/playwright sidecars: a small
     pre-built image dispatched per-extract. Build cost is one-time
     (~250MB image), runtime overhead per extract is ~1-2 seconds.
  3. Adding a new language documenter (C# Roslyn, Go AST) reuses
     this exact pattern — new sidecar, new dispatcher class, no
     architectural changes.

If the sidecar image isn't built, the orchestrator auto-builds it
the first time, mirroring the pytest sidecar's behavior.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


SIDECAR_IMAGE = "bizniz-doc-typescript:latest"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCKERFILE_DIR = _REPO_ROOT / "docker" / "doc-sidecars"


@dataclass
class TypeScriptAstDocumenter:
    """Dispatches the TypeScript documenter sidecar against a
    frontend service workspace.

    Parameters
    ----------
    workspace_root:
        The directory containing the service's source code (e.g.
        ``~/bizniz_projects/<slug>/frontend``).
    service_name:
        Logical name of the service (``"frontend"``). Embedded in
        the output for traceability.
    timeout_s:
        Hard cap on the docker run. Real-world extracts on a typical
        React project run in 3-10 seconds; 60 is plenty of headroom.
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
            "/workspace", self.service_name,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise DocumenterError(
                f"TypeScript documenter timed out after {self.timeout_s}s for "
                f"{self.workspace_root}: {e.stderr or ''}"
            )

        if proc.returncode != 0:
            raise DocumenterError(
                f"TypeScript documenter failed (rc={proc.returncode}) for "
                f"{self.workspace_root}.\nstderr:\n{proc.stderr.strip()[:2000]}"
            )

        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise DocumenterError(
                f"TypeScript documenter produced invalid JSON: {e}\n"
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
        dockerfile = _DOCKERFILE_DIR / "Dockerfile.typescript"
        if not dockerfile.exists():
            raise DocumenterError(
                f"Cannot auto-build TypeScript documenter image: "
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

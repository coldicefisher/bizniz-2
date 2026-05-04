"""Persist documenter output to disk after engineering completes.

Phases 1+2 run documenters in-process and feed the result to the
coder. Phase 4 adds disk persistence so OTHER consumers can read
the same artifact:

  - Phase 6 post-flight (validator runs, then writes results back
    next to the api.json so the next iteration can see what failed)
  - Phase 7 architect.evolve() reads the workspace's existing
    services to plan extensions
  - Future agents (debugger, refactorer, security-review) all read
    from the same `docs/<service>/code/` artifact instead of
    re-extracting

Layout (service-first):

    <project_root>/
      docs/
        <service_name>/
          code/
            api.json            ← documenter output
            extract_meta.json   ← when extracted, which extractor

The artifact is regenerated each time, never edited by hand. This
is the "regenerator, not writer" pattern: humans don't maintain
these files, so they can't drift.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bizniz.architect.types import ServiceDefinition
from bizniz.documenters.python_ast import PythonAstDocumenter
from bizniz.documenters.typescript_ast import (
    TypeScriptAstDocumenter,
    DocumenterError,
)


def docs_dir_for(project_root: Path, service_name: str) -> Path:
    """Return ``<project_root>/docs/<service_name>/code/`` and ensure
    it exists."""
    out = Path(project_root) / "docs" / service_name / "code"
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_service_docs(
    service: ServiceDefinition,
    workspace_root: Path,
    project_root: Path,
    on_status: Optional[callable] = None,
) -> Optional[Path]:
    """Run the language-appropriate documenter on the service's
    workspace and write ``api.json`` to ``docs/<service>/code/``.

    Returns the path written, or ``None`` if no documenter exists
    for this service's language (degrades soft — engineer still
    completes; downstream agents read whatever artifacts exist).

    Soft-fails on documenter errors with a status log; never raises
    upward. The persisted docs are advisory — their absence
    doesn't block the pipeline.
    """
    def _log(msg: str) -> None:
        if on_status:
            on_status(msg)

    lang = (service.language or "").lower()
    documenter = None

    if lang == "python":
        documenter = PythonAstDocumenter(
            workspace_root=Path(workspace_root),
            service_name=service.name,
        )
    elif lang in ("typescript", "javascript"):
        documenter = TypeScriptAstDocumenter(
            workspace_root=Path(workspace_root),
            service_name=service.name,
        )
    # C#, Go, etc. fall through; will be added as profiles in Phase 5.

    if documenter is None:
        _log(
            f"Documenter: no extractor for language '{lang}' on service "
            f"'{service.name}' — skipping doc persistence"
        )
        return None

    out_dir = docs_dir_for(project_root, service.name)
    try:
        out_path = documenter.write(out_dir)
    except DocumenterError as e:
        _log(f"Documenter: extraction failed for '{service.name}' ({e}) — continuing")
        return None
    except Exception as e:
        _log(
            f"Documenter: unexpected error for '{service.name}' "
            f"({type(e).__name__}: {e}) — continuing"
        )
        return None

    # Side-car metadata so future readers know when this was last
    # extracted and by which extractor.
    meta_path = out_dir / "extract_meta.json"
    meta_path.write_text(json.dumps({
        "service": service.name,
        "language": lang,
        "framework": service.framework,
        "extractor": type(documenter).__name__,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "artifact": out_path.name,
    }, indent=2))

    _log(f"Documenter: wrote {out_path.relative_to(project_root)}")
    return out_path

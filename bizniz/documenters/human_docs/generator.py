"""Top-level human-doc generator — Phase 8B orchestrator.

Wires the deterministic renderers + the LLM narrative writer
into a single ``HumanDocsGenerator.run()`` call. Produces the
full ``<project>/docs/`` tree:

```
docs/
├── README.md                   # LLM-driven
├── quickstart.md               # LLM-driven
├── architecture.md             # deterministic
├── infrastructure.md           # deterministic
├── auth.md                     # deterministic
├── api/<service>.md            # deterministic (from OpenAPI)
├── services/<service>.md       # LLM-driven
└── milestones/m<N>.md          # LLM-driven (one per completed milestone)
```

The generator is idempotent: re-running it overwrites all
generated docs. Files outside the generated set (anything an
operator added by hand) are untouched.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from bizniz.architect.types import SystemArchitecture
from bizniz.documenters.human_docs.deterministic import (
    render_api_reference,
    render_architecture,
    render_auth_pointer,
    render_infrastructure,
)
from bizniz.documenters.human_docs.llm_narrative import (
    NarrativeResult,
    NarrativeWriter,
)


class GeneratedDoc(BaseModel):
    """One produced doc file."""
    rel_path: str       # relative to <project>/docs/
    bytes_written: int = 0
    method: str         # "deterministic" or "llm"
    succeeded: bool = True
    error: Optional[str] = None


class HumanDocsResult(BaseModel):
    """End-of-phase summary."""
    duration_s: float = 0.0
    docs_root: str = ""
    docs: List[GeneratedDoc] = Field(default_factory=list)
    skipped_reason: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.skipped_reason is None and all(d.succeeded for d in self.docs)

    def succeeded_count(self) -> int:
        return sum(1 for d in self.docs if d.succeeded)


class MilestoneDocInput(BaseModel):
    """Per-milestone input to ``write_milestone``."""
    index: int
    name: str
    problem_slice: str = ""
    capabilities_summary: str = ""


class HumanDocsGenerator:
    """End-to-end orchestrator for human-readable doc generation.

    Constructor injection:
    - ``project_root`` — where ``docs/`` lives
    - ``architecture`` — the SystemArchitecture artifact
    - ``narrative_writer`` — NarrativeWriter (or test fake)
    - ``compose_yaml`` — string contents of the compose file
    - ``openapi_per_service`` — dict[service_name → openapi dict]
    - ``problem_statement`` — original user input
    - ``milestones`` — list[MilestoneDocInput] for the milestone docs
    """

    def __init__(
        self,
        project_root: Path,
        architecture: SystemArchitecture,
        narrative_writer: NarrativeWriter,
        compose_yaml: str = "",
        openapi_per_service: Optional[Dict[str, dict]] = None,
        problem_statement: str = "",
        milestones: Optional[List[MilestoneDocInput]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._architecture = architecture
        self._narrative_writer = narrative_writer
        self._compose_yaml = compose_yaml
        self._openapi_per_service = dict(openapi_per_service or {})
        self._problem_statement = problem_statement
        self._milestones = list(milestones or [])
        self._on_status = on_status

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    @property
    def _docs_root(self) -> Path:
        return self._project_root / "docs"

    def _write(
        self, rel_path: str, content: str, method: str,
        succeeded: bool = True, error: Optional[str] = None,
    ) -> GeneratedDoc:
        path = self._docs_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(content, encoding="utf-8")
            return GeneratedDoc(
                rel_path=rel_path,
                bytes_written=len(content.encode("utf-8")),
                method=method,
                succeeded=succeeded,
                error=error,
            )
        except OSError as e:
            return GeneratedDoc(
                rel_path=rel_path,
                bytes_written=0,
                method=method,
                succeeded=False,
                error=f"OSError: {e}",
            )

    def run(self) -> HumanDocsResult:
        """Generate every doc. Never raises — failures are recorded
        on individual ``GeneratedDoc`` records.

        Returns ``HumanDocsResult`` with the docs list + overall
        ``passed`` flag (true when every doc generated successfully).
        """
        t0 = time.time()
        self._docs_root.mkdir(parents=True, exist_ok=True)
        result = HumanDocsResult(docs_root=str(self._docs_root))

        # ── Deterministic docs ───────────────────────────────────
        self._log("HumanDocsGenerator: rendering architecture.md...")
        result.docs.append(self._write(
            "architecture.md",
            render_architecture(self._architecture),
            method="deterministic",
        ))

        self._log("HumanDocsGenerator: rendering infrastructure.md...")
        result.docs.append(self._write(
            "infrastructure.md",
            render_infrastructure(self._compose_yaml, self._architecture),
            method="deterministic",
        ))

        self._log("HumanDocsGenerator: rendering auth.md...")
        result.docs.append(self._write(
            "auth.md",
            render_auth_pointer(self._architecture),
            method="deterministic",
        ))

        for svc_name, openapi in self._openapi_per_service.items():
            self._log(f"HumanDocsGenerator: rendering api/{svc_name}.md...")
            result.docs.append(self._write(
                f"api/{svc_name}.md",
                render_api_reference(svc_name, openapi),
                method="deterministic",
            ))

        # ── LLM-driven docs ──────────────────────────────────────
        readme = self._narrative_writer.write_readme(
            self._architecture, self._problem_statement,
        )
        result.docs.append(self._write(
            "README.md",
            readme.content,
            method="llm",
            succeeded=readme.succeeded,
            error=readme.error,
        ))

        quickstart = self._narrative_writer.write_quickstart(
            self._architecture, self._compose_yaml[:2000],
        )
        result.docs.append(self._write(
            "quickstart.md",
            quickstart.content,
            method="llm",
            succeeded=quickstart.succeeded,
            error=quickstart.error,
        ))

        for svc in self._architecture.services:
            # Skip infrastructure-only services (db, cache, queue).
            if (svc.service_type or "").lower() not in (
                "backend", "frontend", "worker", "auth",
            ):
                continue
            svc_doc = self._narrative_writer.write_service(svc, self._architecture)
            result.docs.append(self._write(
                f"services/{svc.name}.md",
                svc_doc.content,
                method="llm",
                succeeded=svc_doc.succeeded,
                error=svc_doc.error,
            ))

        for ms in self._milestones:
            ms_doc = self._narrative_writer.write_milestone(
                milestone_index=ms.index,
                milestone_name=ms.name,
                milestone_problem_slice=ms.problem_slice,
                capabilities_summary=ms.capabilities_summary,
            )
            result.docs.append(self._write(
                f"milestones/m{ms.index}.md",
                ms_doc.content,
                method="llm",
                succeeded=ms_doc.succeeded,
                error=ms_doc.error,
            ))

        result.duration_s = time.time() - t0
        self._log(
            f"HumanDocsGenerator: done in {result.duration_s:.1f}s — "
            f"{result.succeeded_count()}/{len(result.docs)} succeeded"
        )
        return result

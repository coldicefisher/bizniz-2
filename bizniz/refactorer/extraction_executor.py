"""Extraction executor — Phase F.

Given an ``ExtractionPlan`` (Phase E output), invoke an LLM with
file-edit tools to:

1. Read the source files at the duplicate line ranges
2. Move the shared code to ``<project>/core/<lang>/<path>``
3. Rewrite consumer imports (Python ``from python_core.<...>``,
   TypeScript ``from "ts_core/<...>"``)
4. Verify the changes are syntactically valid via best-effort
   import / parse checks
5. Run the project's test suite via the injected runner
6. On test failure, git-revert the extraction and surface the
   failure

The executor doesn't open files itself — Claude CLI with
Edit/Write/Read tools does the actual editing. The executor builds
the prompt, runs the subprocess, parses the structured result, and
arbitrates with the git/test layers.

Pattern mirrors ClaudeCliCoder + ProUXDesigner fix dispatch —
LLM as the surgical instrument, deterministic glue around it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Literal, Optional

from pydantic import BaseModel, Field

from bizniz.refactorer.extraction_planner import ExtractionPlan


# ── Output schema ────────────────────────────────────────────────


Status = Literal["applied", "no_changes", "reverted", "failed"]


class ExtractionResult(BaseModel):
    """One executor invocation's outcome."""
    plan_hash: str
    status: Status = "failed"
    files_written: List[str] = Field(default_factory=list)
    files_modified: List[str] = Field(default_factory=list)
    test_passed: bool = False
    test_output_tail: str = ""
    summary: str = ""
    notes: List[str] = Field(default_factory=list)
    commit_hash: Optional[str] = None  # If applied + tests passed.


# ── Prompts ──────────────────────────────────────────────────────


_EXECUTOR_SYSTEM_PROMPT = """You are a senior refactor engineer extracting duplicated code to a shared core library. The extraction has already been identified — your job is to perform it cleanly.

You have these tools: Read, Edit, Write, Glob, Grep, Bash.

Workflow:

1. Read the source files at the duplicate line ranges (provided in the user prompt).
2. Identify the smallest meaningful unit: a function, class, or block of constants — whatever is the natural boundary around the duplicated code.
3. Create a new file at the target ``core/<lang>/<path>`` containing JUST that unit. The unit must be standalone — no imports of service-local code. If it needs supporting types, EXTRACT THOSE TOO into the same target file.
4. In each source file, REPLACE the duplicated code with an import from the new core path.
   - Python: ``from python_core.<path> import <symbol>``
   - TypeScript: ``import { <symbol> } from "ts_core/<path>"``
5. Verify no service-local imports remain that would break.
6. Emit the JSON result described below.

**Critical rules:**

- Do NOT change the symbol's PUBLIC API (function signature, class shape, exports). Consumers must keep working.
- Do NOT extract code that has service-specific dependencies baked in (DB session, HTTP client with config, framework-specific decorators like ``@app.get``). If you detect this, abort with ``status: "no_changes"`` and explain in ``summary``.
- Do NOT touch test files for this extraction. Tests for the extracted code can be added later as a separate task.
- Do NOT delete the original files — only delete the duplicated REGIONS within them and replace with imports.

Output STRICT JSON when finished, no commentary:

```json
{
  "status": "applied|no_changes|failed",
  "files_written": ["/abs/path/to/new/core/file"],
  "files_modified": ["/abs/path/to/edited/source/file"],
  "summary": "one-sentence description of the extraction",
  "notes": ["optional supplementary observations, esp. service-local dependencies you saw"]
}
```

`status` rules:
- "applied" — extraction made; both writes and modifications happened
- "no_changes" — couldn't extract cleanly (service-local deps, not actually duplicated, etc.); files untouched
- "failed" — partial / broken state; report what you tried

The Refactorer agent runs tests after you finish. If they fail, your edits get git-reverted automatically — so be willing to attempt the extraction even when uncertain. Conservative "no_changes" is fine when the extraction is genuinely wrong, but a clean failed test is recoverable.
"""


_EXECUTOR_USER_TEMPLATE = """Extract this duplicated code to the shared core library.

**Duplicate identity:** hash={plan_hash}, language={language}
**Target core path:** {target_path}
**Services involved:** {services}
**Files containing duplicate (with line ranges):**

{occurrences_block}

**Risk score:** {risk_score:.2f}
**Notes from planner:**
{planner_notes}

Read the source files, identify the natural unit, create the core file, rewrite the source files to import from core, then emit the JSON result per the system prompt.
"""


def _build_user_prompt(plan: ExtractionPlan) -> str:
    occurrences_block: List[str] = []
    for f in plan.source_files:
        occurrences_block.append(f"- {f}  (token block ~{plan.token_count} tokens)")
    notes_block = "\n".join(f"- {n}" for n in plan.notes) if plan.notes else "(none)"
    return _EXECUTOR_USER_TEMPLATE.format(
        plan_hash=plan.duplicate_hash,
        language=plan.language,
        target_path=plan.suggested_core_path,
        services=", ".join(plan.services_involved) or "(none)",
        occurrences_block="\n".join(occurrences_block),
        risk_score=plan.risk_score,
        planner_notes=notes_block,
    )


# ── Result parsing ───────────────────────────────────────────────


def _parse_executor_json(raw: dict, plan: ExtractionPlan) -> ExtractionResult:
    status = raw.get("status", "failed")
    if status not in ("applied", "no_changes", "failed"):
        status = "failed"
    files_written = raw.get("files_written") or []
    files_modified = raw.get("files_modified") or []
    if not isinstance(files_written, list):
        files_written = []
    if not isinstance(files_modified, list):
        files_modified = []
    notes = raw.get("notes") or []
    if not isinstance(notes, list):
        notes = []
    return ExtractionResult(
        plan_hash=plan.duplicate_hash,
        status=status,
        files_written=[str(p) for p in files_written if isinstance(p, str)],
        files_modified=[str(p) for p in files_modified if isinstance(p, str)],
        summary=str(raw.get("summary") or ""),
        notes=[str(n) for n in notes if isinstance(n, str)],
    )


# ── Executor ─────────────────────────────────────────────────────


class ExtractionExecutor:
    """Drives one extraction end-to-end.

    All collaborators are constructor-injected for testability:
    ``llm_invoker`` (Claude CLI), ``test_runner`` (project test suite),
    ``git_ops`` (commit / revert).
    """

    def __init__(
        self,
        project_root: Path,
        command: str = "claude",
        on_status: Optional[Callable[[str], None]] = None,
        llm_invoker: Optional[Callable[[ExtractionPlan, str], Optional[dict]]] = None,
        test_runner: Optional[Callable[[], "TestRunResult"]] = None,
        git_ops: Optional["GitOps"] = None,
        additional_args: Optional[List[str]] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._command = command
        self._on_status = on_status
        self._llm_invoker = llm_invoker or self._default_llm_invoker
        self._test_runner = test_runner or _null_test_runner
        self._git_ops = git_ops or _NullGitOps()
        self._additional_args = list(additional_args or [])

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def execute(self, plan: ExtractionPlan) -> ExtractionResult:
        """Apply one extraction plan. Returns a result even on
        failure — never raises.

        Flow:
        1. LLM does the extraction (file edits)
        2. If status != applied, return with status preserved
        3. Run tests
        4. On test failure, git-revert and mark status=reverted
        5. On test pass, git-commit and mark status=applied
        """
        # Record the pre-edit git rev so we can revert exactly.
        pre_rev = self._git_ops.head_rev()

        user_prompt = _build_user_prompt(plan)
        self._log(
            f"ExtractionExecutor: dispatching {plan.duplicate_hash} "
            f"({plan.language}, {len(plan.source_files)} files)..."
        )
        parsed = self._llm_invoker(plan, user_prompt)
        if parsed is None:
            return ExtractionResult(
                plan_hash=plan.duplicate_hash,
                status="failed",
                summary="LLM returned no parseable JSON",
            )
        result = _parse_executor_json(parsed, plan)

        if result.status != "applied":
            self._log(
                f"ExtractionExecutor: {plan.duplicate_hash} "
                f"{result.status} — {result.summary}"
            )
            return result

        # Run tests.
        self._log(
            f"ExtractionExecutor: {plan.duplicate_hash} edits applied "
            f"({len(result.files_written)} new, "
            f"{len(result.files_modified)} modified) — running tests..."
        )
        test_result = self._test_runner()
        result.test_passed = test_result.passed
        result.test_output_tail = test_result.output_tail

        if not test_result.passed:
            self._log(
                f"ExtractionExecutor: {plan.duplicate_hash} tests FAILED — "
                f"reverting"
            )
            if pre_rev is not None:
                self._git_ops.revert_to(pre_rev)
            result.status = "reverted"
            return result

        # Tests passed → commit the extraction.
        commit_msg = (
            f"refactor: extract {plan.suggested_core_path} "
            f"({plan.duplicate_hash[:8]}, "
            f"{len(plan.services_involved)} services)"
        )
        commit_hash = self._git_ops.commit_all(commit_msg)
        result.commit_hash = commit_hash
        self._log(
            f"ExtractionExecutor: {plan.duplicate_hash} committed "
            f"as {commit_hash[:8] if commit_hash else '?'}"
        )
        return result

    # ── Default Claude CLI invoker ───────────────────────────────

    def _default_llm_invoker(
        self,
        plan: ExtractionPlan,
        user_prompt: str,
    ) -> Optional[dict]:
        if shutil.which(self._command) is None:
            self._log(
                f"ExtractionExecutor: {self._command!r} not on PATH"
            )
            return None
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", _EXECUTOR_SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Edit Write Read Bash Glob Grep",
            "--add-dir", str(self._project_root),
        ] + self._additional_args
        try:
            proc = subprocess.run(
                cmd, input=user_prompt, capture_output=True,
                text=True, timeout=900,
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        try:
            envelope = json.loads(proc.stdout)
        except Exception:
            return None
        inner = envelope.get("result")
        if not isinstance(inner, str):
            return None
        start = inner.find("{")
        end = inner.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(inner[start:end + 1])
        except Exception:
            return None


# ── Test runner + git ops abstractions ───────────────────────────


class TestRunResult(BaseModel):
    """Outcome of running the project's test suite."""
    # Tell pytest this isn't a test class. Without this, the leading
    # ``Test`` prefix triggers collection + a warning since the class
    # has a Pydantic-managed __init__.
    __test__ = False
    passed: bool = False
    output_tail: str = ""


def _null_test_runner() -> TestRunResult:
    """Default: assume tests pass. The Refactorer agent (Phase G)
    injects a real runner that invokes pytest / npm test."""
    return TestRunResult(passed=True)


class GitOps:
    """Interface for the executor's git interactions. Refactorer
    agent (Phase G) supplies a real implementation backed by
    ``bizniz.driver.project_git.ProjectGit``."""

    def head_rev(self) -> Optional[str]:
        raise NotImplementedError

    def commit_all(self, message: str) -> Optional[str]:
        raise NotImplementedError

    def revert_to(self, rev: str) -> None:
        raise NotImplementedError


class _NullGitOps(GitOps):
    """Default no-op implementation — useful for tests / dry runs."""

    def head_rev(self) -> Optional[str]:
        return None

    def commit_all(self, message: str) -> Optional[str]:
        return None

    def revert_to(self, rev: str) -> None:
        return

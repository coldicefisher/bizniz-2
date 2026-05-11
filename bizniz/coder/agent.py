"""v2.5 Coder agent — narrow-context per-issue tool-loop.

ONE issue per call. Combines v1's Coder + Tester + QuickDebugger
roles into one agent. Adds the deterministic ``validate_symbols``
step between code-write and test-write — the hallucination firewall.

Inherits from ``ToolLoopAgent`` (v2 ABC) so it gets:
  - The standard tool-loop (system + initial + N turns)
  - Stall detection (3-of-5 sig matches → escalate)
  - JSON-schema response forcing
  - Per-call cost tracking via the global tracker

Construction is once per orchestrator. Each ``code_issue()`` call
runs the loop fresh against a single Issue.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from bizniz.architect.types import SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.coder.prompts.initial_context import build_coder_initial_context
from bizniz.coder.prompts.schema import CODER_ACTION_SCHEMA
from bizniz.coder.prompts.system_prompt import CODER_SYSTEM_PROMPT
from bizniz.coder.symbol_validator import validate_files
from bizniz.coder.types import CoderError, CoderResult, Issue
from bizniz.lib.tool_loop_agent import (
    TerminalActionRejected, ToolHandler, ToolLoopAgent,
    ToolLoopAgentStalledError,
)
from bizniz.lib.tools.container import build_container_handlers
from bizniz.lib.tools.database import build_database_handlers
from bizniz.lib.tools.discovery import build_discovery_handlers
from bizniz.lib.tools.file_io import build_file_io_handlers
from bizniz.lib.tools.jwt import build_jwt_handlers
from bizniz.lib.tools.test_runner import build_test_handlers
from bizniz.quality_engineer.types import EnrichedSpec
from bizniz.workspace.base_workspace import BaseWorkspace


class Coder(ToolLoopAgent):
    """Per-issue narrow-context coder + tester."""

    def __init__(
        self,
        client: BaseAIClient,
        workspace: BaseWorkspace,
        compose_path: str,
        target_service: str,
        on_status: Optional[Callable[[str], None]] = None,
        tool_iterations: int = 30,
        timeout_seconds: int = 1200,
        base_url: Optional[str] = None,
        workspace_name: Optional[str] = None,
        runner: str = "pytest",
    ):
        super().__init__(
            client=client,
            workspace=workspace,
            on_status=on_status,
            tool_iterations=tool_iterations,
            timeout_seconds=timeout_seconds,
            history_window=0,  # full history (caching does the work)
        )
        self._compose_path = compose_path
        self._target_service = target_service
        self._base_url = base_url
        self._runner = runner
        # ServicePlanner sometimes emits paths like "backend/app/users.py"
        # treating workspace as project-rooted; the actual workspace IS
        # the service dir, so unstripped paths land at "backend/backend/
        # app/users.py" and the FastAPI skeleton can't import them at
        # runtime. Track the prefix and strip in write_file / run_tests.
        self._workspace_prefix = (workspace_name or target_service).strip("/")
        # Per-call state — set in code_issue()
        self._issue: Optional[Issue] = None
        self._target_files_written: List[str] = []
        self._test_files_written: List[str] = []
        self._handlers: Dict[str, ToolHandler] = {}
        self._last_test_output: str = ""

    # ── ToolLoopAgent contract ─────────────────────────────────────────

    @property
    def system_prompt(self) -> str:
        return CODER_SYSTEM_PROMPT

    @property
    def action_schema(self) -> dict:
        return CODER_ACTION_SCHEMA

    @property
    def terminal_action(self) -> str:
        return "submit_code"

    def tool_handlers(self) -> Dict[str, ToolHandler]:
        return self._handlers

    def _log_action(self, agent_name, action_type, action, turn=None, total=None):
        """Override to prefix every action log with ``[service:issue_id
        iter N/M]`` so the user can see which issue is grinding and
        how many iterations it has left."""
        bits = []
        for key in ("path", "url", "command"):
            v = action.get(key)
            if v:
                v_str = str(v)
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
                bits.append(f"{key}={v_str!r}")
        suffix = f" ({', '.join(bits)})" if bits else ""

        scope = f"{self._target_service}"
        if self._issue is not None:
            scope = f"{self._target_service}:{self._issue.id}"
        iter_str = ""
        if turn is not None and total is not None:
            iter_str = f" iter {turn}/{total}"
        self._log(f"[{scope}{iter_str}] {action_type}{suffix}")

    def parse_terminal_action(self, action: dict) -> CoderResult:
        if self._issue is None:
            raise CoderError("submit_code reached without an active issue")

        status = action.get("status") or "passed"
        # Green-tests gate: if the LLM claims "passed", verify it
        # actually ran tests and they were green. Without this, the
        # Coder can submit fake passes — the only deterministic
        # signal we have is whether ``run_tests`` was invoked and
        # what its output looked like.
        if status == "passed":
            rejection = self._reject_if_tests_not_green()
            if rejection:
                raise TerminalActionRejected(rejection)

        return CoderResult(
            issue_id=self._issue.id,
            status=status,
            target_files_written=list(self._target_files_written),
            test_files_written=list(self._test_files_written),
            summary=action.get("summary") or "",
            notes=list(action.get("notes") or []),
            last_test_output_tail=self._last_test_output[-2000:],
        )

    def _reject_if_tests_not_green(self) -> Optional[str]:
        """Return a rejection message if the model claims ``passed``
        but ``run_tests`` either wasn't called or its output doesn't
        carry the deterministic ``TESTS PASSED`` verdict prefix.

        The ``run_tests`` handler in ``lib/tools/test_runner.py``
        prefixes its output with ``TESTS PASSED`` iff pytest exited 0,
        and ``TESTS FAILED`` otherwise. That prefix is the
        deterministic signal — we own it and the LLM can't fabricate
        it because ``self._last_test_output`` is captured by our
        wrapper directly from the handler.

        Limitation: a green output here means *the last pytest
        invocation* exited 0. It does NOT prove the issue's specific
        test files were exercised — the LLM could run an unrelated
        green test directory. We accept that risk at this layer and
        rely on QualityEngineer.review (post-flight, full spec
        context) to catch coverage gaps.
        """
        if not self._last_test_output:
            return (
                "submit_code rejected: you claimed status='passed' but "
                "run_tests was never called this run. Invoke run_tests "
                "with the path to the test files for this issue, "
                "confirm the output starts with 'TESTS PASSED', then "
                "resubmit. If tests are red, fix the code and try "
                "again. If you genuinely cannot get tests to pass, "
                "submit with status='partial' or status='failed' and "
                "describe the blocker in the summary."
            )
        if not self._last_test_output.lstrip().startswith("TESTS PASSED"):
            tail = self._last_test_output[-1000:]
            return (
                "submit_code rejected: you claimed status='passed' but "
                "the last run_tests output does not start with the "
                "deterministic 'TESTS PASSED' verdict (pytest exit 0). "
                "Read the output below, fix the issue, run_tests "
                "again, and only submit status='passed' when the run "
                "exits clean. If you cannot resolve it, submit "
                "status='partial' with a summary of the blocker.\n\n"
                f"--- last test output (tail) ---\n{tail}"
            )
        return None

    # ── Public ─────────────────────────────────────────────────────────

    def code_issue(
        self,
        issue: Issue,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        auth_contract: Optional[str] = None,
        workspace_summary: Optional[str] = None,
        skeleton_md: Optional[str] = None,
    ) -> CoderResult:
        """Run the Coder against one issue. Returns CoderResult.

        On stall (same action 3-of-5), raises ``ToolLoopAgentStalledError``
        — the orchestrator catches and decides to escalate model tier
        or move on.
        """
        self._log(f"Coder: {issue.id} — {issue.title} (service={issue.service})")
        self._issue = issue
        self._target_files_written = []
        self._test_files_written = []
        self._last_test_output = ""
        # Default tool service to the issue's service so commands run
        # in the right container by default.
        self._handlers = self._build_handlers(issue.service)

        initial = build_coder_initial_context(
            issue=issue,
            architecture=architecture,
            enriched_spec=enriched_spec,
            auth_contract=auth_contract,
            workspace_summary=workspace_summary,
            skeleton_md=skeleton_md,
        )
        return self.run(initial)

    # ── Tool surface ───────────────────────────────────────────────────

    def _build_handlers(self, target_service: str) -> Dict[str, ToolHandler]:
        h: Dict[str, ToolHandler] = {}
        h.update(build_file_io_handlers(self._workspace))
        h.update(build_discovery_handlers(self._workspace))
        h.update(build_container_handlers(self._compose_path, target_service))
        h.update(
            build_test_handlers(
                compose_path=self._compose_path,
                workspace_path=Path(self._workspace.root)
                if hasattr(self._workspace, "root") else Path("."),
                target_service=target_service,
                base_url=self._base_url,
                runner=self._runner,
            )
        )
        h.update(build_database_handlers(self._compose_path))
        h.update(build_jwt_handlers())

        # Wrap write_file to (1) strip workspace_name prefix from
        # paths the LLM emitted as project-rooted, and (2) track which
        # target/test files we've written.
        original_write = h["write_file"]

        def write_file_tracked(action: dict) -> str:
            normalized_path = self._strip_workspace_prefix(
                action.get("path") or ""
            )
            if normalized_path != action.get("path"):
                action = {**action, "path": normalized_path}
            result = original_write(action)
            path = normalized_path
            if self._issue is not None:
                # Match the issue's declared target/test files using
                # both the LLM's path (raw) and the normalized path so
                # tracking stays correct regardless of which form the
                # ServicePlanner emitted.
                target_set = {self._strip_workspace_prefix(p)
                              for p in self._issue.target_files}
                test_set = {self._strip_workspace_prefix(p)
                            for p in self._issue.test_files}
                if path in target_set:
                    if path not in self._target_files_written:
                        self._target_files_written.append(path)
                elif path in test_set:
                    if path not in self._test_files_written:
                        self._test_files_written.append(path)
            return result

        h["write_file"] = write_file_tracked

        # Wrap run_tests so we (1) strip workspace_name prefix from
        # the path arg and (2) capture the last output for the result.
        original_run_tests = h["run_tests"]

        def run_tests_tracked(action: dict) -> str:
            raw_path = action.get("path") or ""
            normalized = self._strip_workspace_prefix(raw_path)
            if normalized != raw_path:
                action = {**action, "path": normalized}
            result = original_run_tests(action)
            self._last_test_output = result
            return result

        h["run_tests"] = run_tests_tracked

        # The new bit: validate_symbols action.
        h["validate_symbols"] = self._handle_validate_symbols

        return h

    # ── Path normalization ────────────────────────────────────────────

    def _strip_workspace_prefix(self, path: str) -> str:
        """Strip a leading ``<workspace_name>/`` from ``path``.

        ServicePlanner sometimes emits target_files like
        ``"backend/app/users.py"`` thinking workspaces are project-
        rooted. The Coder's workspace IS the service dir, so without
        stripping the file lands at ``backend/backend/app/users.py``
        and the FastAPI skeleton's ``from app.models.user import User``
        looks at ``app/app/models/user.py`` and 404s at runtime.
        Idempotent on already-relative paths.
        """
        prefix = self._workspace_prefix + "/"
        if path.startswith(prefix):
            return path[len(prefix):]
        return path

    # ── validate_symbols handler ──────────────────────────────────────

    def _handle_validate_symbols(self, action: dict) -> str:
        """Run the deterministic AST-walk validator over the target_files
        written so far. If the issue is non-Python, returns a note and
        passes through (no symbol validation for TypeScript yet).
        """
        if self._issue is None:
            return "ERROR: validate_symbols called without an active issue."
        if not self._target_files_written:
            return (
                "validate_symbols: no target_files have been written yet. "
                "Write your source files first (write_file), then run this."
            )
        if (self._issue.language or "").lower() not in ("python",):
            return (
                f"validate_symbols: skipped — language='{self._issue.language}' "
                f"is not yet supported by the AST validator (Python only "
                f"in v2.5.0). Proceed to write tests; rely on run_tests "
                f"for verification."
            )
        # Resolve workspace root to validate against.
        ws_root = Path(self._workspace.root) if hasattr(self._workspace, "root") else Path(".")
        # Map issue-relative paths to absolute via workspace.path() if possible.
        abs_paths: List[Path] = []
        for rel in self._target_files_written:
            try:
                p = self._workspace.path(rel)
                abs_paths.append(Path(p))
            except Exception:
                abs_paths.append(ws_root / rel)
        report = validate_files(abs_paths, ws_root)
        return report.render()

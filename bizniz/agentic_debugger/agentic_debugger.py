"""
AgenticDebugger

An agentic debugging agent that iteratively explores the codebase using tools
(view_file, list_directory, run_tests) to diagnose test failures and optionally
produce direct code fixes.

Complements the Autodebugger (quick per-iteration diagnosis) with a more
capable, exploratory agent for complex debugging scenarios.

The debugger simulates tool calling via structured JSON responses — the LLM
returns an action object per turn, the debugger executes it, and feeds the
result back as a new user message.
"""

import json
import subprocess
import time
from typing import Optional, Callable, List, Dict

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import ExecutionCallSpec
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.utils.json import clean_llm_json
from bizniz.tools.discovery_tools import (
    tool_view_file,
    tool_list_directory,
    tool_search_files,
    build_filtered_file_tree,
    TREE_EXCLUDE_DIRS,
    TREE_MAX_FILES,
)

from bizniz.agentic_debugger.types import (
    AgenticDiagnosis,
    CodeFix,
    AgenticDebuggerError,
    AgenticDebuggerTimeoutError,
    AgenticDebuggerGaveUpError,
    AgenticDebuggerBadResponseError,
)
from bizniz.agentic_debugger.prompts.system_prompt import AGENTIC_DEBUGGER_SYSTEM_PROMPT
from bizniz.agentic_debugger.prompts.schema import AgenticDebuggerActionSchema


class AgenticDebugger:
    """
    Agentic debugging agent with tool-use capabilities.

    Parameters
    ----------
    client:
        AI client instance. Should be a dedicated instance (not shared with
        autocoder/autotester) to avoid message history contamination.
    workspace:
        The workspace to explore files in.
    environment:
        Execution environment for running tests.
    max_turns:
        Maximum number of tool-call turns before forcing a diagnosis.
    timeout_seconds:
        Maximum wall-clock time for the debugging session.
    on_status_message:
        Optional callback for human-readable status updates.
    """

    def __init__(
        self,
        client: BaseAIClient,
        workspace: BaseWorkspace,
        environment: BaseExecutionEnvironment,
        max_turns: int = 15,
        timeout_seconds: int = 600,
        on_status_message: Optional[Callable[[str], None]] = None,
    ):
        self._client = client
        self._workspace = workspace
        self._environment = environment
        self._max_turns = max_turns
        self._timeout_seconds = timeout_seconds
        self._on_status_message = on_status_message

    def diagnose(
        self,
        error_output: str,
        source_files: Dict[str, str],
        test_files: Dict[str, str],
        architecture_context: str = "",
        repair_history: Optional[List[str]] = None,
    ) -> AgenticDiagnosis:
        """
        Run the agentic debugging loop to diagnose a test failure.

        Parameters
        ----------
        error_output:
            The full pytest error output.
        source_files:
            Dict mapping filepath to content for source files under test.
        test_files:
            Dict mapping filepath to content for test files.
        architecture_context:
            Optional architecture plan for context.
        repair_history:
            Optional list of previous repair attempt summaries.

        Returns
        -------
        AgenticDiagnosis with root cause, fix plan, and optional code fixes.
        """
        if repair_history is None:
            repair_history = []

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        log("AgenticDebugger: starting diagnosis...")

        # Build initial context
        initial_context = self._build_initial_context(
            error_output=error_output,
            source_files=source_files,
            test_files=test_files,
            architecture_context=architecture_context,
            repair_history=repair_history,
        )

        # Build conversation
        messages = [
            {"role": "system", "content": AGENTIC_DEBUGGER_SYSTEM_PROMPT},
            {"role": "user", "content": initial_context},
        ]

        start_time = time.time()
        parse_failures = 0
        max_parse_failures = 3

        for turn in range(1, self._max_turns + 1):
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed > self._timeout_seconds:
                log(f"AgenticDebugger: timeout after {int(elapsed)}s — forcing diagnosis")
                messages.append({
                    "role": "user",
                    "content": (
                        "TIME LIMIT REACHED. You must submit your diagnosis NOW. "
                        "Use action 'submit_fix' with your best diagnosis based on "
                        "what you've learned so far."
                    ),
                })

            # Call LLM
            try:
                text, _, _ = self._client.get_text(
                    messages=messages,
                    use_message_history=False,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=AgenticDebuggerActionSchema,
                )
            except Exception as e:
                log(f"AgenticDebugger: LLM call failed ({type(e).__name__}: {e})")
                parse_failures += 1
                if parse_failures >= max_parse_failures:
                    raise AgenticDebuggerBadResponseError(
                        f"LLM call failed {max_parse_failures} times: {e}"
                    )
                continue

            if not text or not text.strip():
                parse_failures += 1
                if parse_failures >= max_parse_failures:
                    raise AgenticDebuggerBadResponseError("LLM returned empty response")
                continue

            # Parse action
            try:
                text = clean_llm_json(text)
                action = json.loads(text)
            except (json.JSONDecodeError, Exception) as e:
                parse_failures += 1
                log(f"AgenticDebugger: failed to parse response ({e})")
                if parse_failures >= max_parse_failures:
                    raise AgenticDebuggerBadResponseError(
                        f"Failed to parse LLM response after {max_parse_failures} attempts"
                    )
                messages.append({
                    "role": "assistant",
                    "content": text,
                })
                messages.append({
                    "role": "user",
                    "content": "Your response was not valid JSON. Please try again.",
                })
                continue

            # Add assistant response to conversation
            messages.append({"role": "assistant", "content": text})

            action_type = action.get("action", "")
            thinking = action.get("thinking", "")
            path = action.get("path", "")

            # Handle actions
            if action_type == "submit_fix":
                log(f"AgenticDebugger: diagnosis submitted — {action.get('root_cause_category', 'unknown')} "
                    f"(confidence: {action.get('confidence', 'unknown')})")

                code_fixes = []
                for fix in action.get("code_fixes", []):
                    if fix.get("filepath") and fix.get("new_content"):
                        code_fixes.append(CodeFix(
                            filepath=fix["filepath"],
                            new_content=fix["new_content"],
                        ))

                return AgenticDiagnosis(
                    diagnosis=action.get("diagnosis", ""),
                    root_cause_category=action.get("root_cause_category", "other"),
                    fix_target=action.get("fix_target", "code"),
                    affected_files=action.get("affected_files", []),
                    fix_plan=action.get("fix_plan", []),
                    suggested_approach=action.get("suggested_approach", ""),
                    missing_packages=action.get("missing_packages", []),
                    confidence=action.get("confidence", "medium"),
                    code_fixes=code_fixes,
                )

            elif action_type == "view_file":
                log(f"AgenticDebugger: viewing {path}")
                result = self._tool_view_file(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: view_file(\"{path}\")]\n{result}",
                })

            elif action_type == "list_directory":
                log(f"AgenticDebugger: listing {path or '.'}")
                result = self._tool_list_directory(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: list_directory(\"{path}\")]\n{result}",
                })

            elif action_type == "search_files":
                log(f"AgenticDebugger: searching for '{path}'")
                result = self._tool_search_files(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: search_files(\"{path}\")]\n{result}",
                })

            elif action_type == "run_command":
                log(f"AgenticDebugger: running command: {path[:80]}")
                result = self._tool_run_command(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: run_command(\"{path}\")]\n{result}",
                })

            elif action_type == "run_tests":
                log(f"AgenticDebugger: running tests {path}")
                result = self._tool_run_tests(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: run_tests(\"{path}\")]\n{result}",
                })

            else:
                messages.append({
                    "role": "user",
                    "content": f"Unknown action '{action_type}'. Use one of: view_file, list_directory, search_files, run_command, run_tests, submit_fix",
                })

        # Exhausted turns without a diagnosis — force one
        log("AgenticDebugger: max turns reached — forcing diagnosis")
        messages.append({
            "role": "user",
            "content": (
                "You have used all available turns. You MUST submit your diagnosis now. "
                "Use action 'submit_fix' with your best diagnosis."
            ),
        })

        # One final call
        try:
            text, _, _ = self._client.get_text(
                messages=messages,
                use_message_history=False,
                response_format=ResponseFormat.JSON_SCHEMA,
                schema=AgenticDebuggerActionSchema,
            )
            text = clean_llm_json(text)
            action = json.loads(text)

            if action.get("action") == "submit_fix":
                code_fixes = []
                for fix in action.get("code_fixes", []):
                    if fix.get("filepath") and fix.get("new_content"):
                        code_fixes.append(CodeFix(
                            filepath=fix["filepath"],
                            new_content=fix["new_content"],
                        ))

                return AgenticDiagnosis(
                    diagnosis=action.get("diagnosis", ""),
                    root_cause_category=action.get("root_cause_category", "other"),
                    fix_target=action.get("fix_target", "code"),
                    affected_files=action.get("affected_files", []),
                    fix_plan=action.get("fix_plan", []),
                    suggested_approach=action.get("suggested_approach", ""),
                    missing_packages=action.get("missing_packages", []),
                    confidence=action.get("confidence", "low"),
                    code_fixes=code_fixes,
                )
        except Exception:
            pass

        # Absolute fallback
        return AgenticDiagnosis(
            diagnosis="Debugger could not determine root cause within turn/time limits.",
            root_cause_category="other",
            fix_target="code",
            confidence="low",
        )

    # ── Tool implementations ──────────────────────────────────────────────────

    def _tool_view_file(self, path: str) -> str:
        return tool_view_file(self._workspace, path)

    def _tool_list_directory(self, path: str) -> str:
        return tool_list_directory(self._workspace, path)

    def _tool_search_files(self, pattern: str) -> str:
        return tool_search_files(self._workspace, pattern)

    def _tool_run_command(self, command: str) -> str:
        """Execute a shell command in the workspace directory."""
        try:
            if not command:
                return "ERROR: No command provided."
            workspace_root = str(self._workspace.root)
            result = subprocess.run(
                command,
                shell=True,
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=60,
            )
            output_parts = []
            if result.stdout:
                output_parts.append(result.stdout)
            if result.stderr:
                output_parts.append(f"STDERR:\n{result.stderr}")
            output_parts.append(f"(exit code: {result.returncode})")

            output = "\n".join(output_parts)
            # Truncate very long output
            if len(output) > 10000:
                output = output[:10000] + "\n\n... (output truncated)"
            return output
        except subprocess.TimeoutExpired:
            return "ERROR: Command timed out after 60 seconds."
        except Exception as e:
            return f"ERROR: Command failed: {e}"

    def _tool_run_tests(self, path: str) -> str:
        """Run pytest on the specified test files."""
        try:
            if not path:
                return "ERROR: No test file paths provided."

            test_paths = path.strip().split()
            abs_paths = [
                str(self._workspace.path(tp)) for tp in test_paths
            ]

            call_spec = ExecutionCallSpec(symbol="pytest", args=abs_paths)
            result = self._environment.execute(code="", call_spec=call_spec)

            output_parts = []
            if result.success:
                output_parts.append("TESTS PASSED")
            else:
                output_parts.append("TESTS FAILED")

            if result.stdout:
                output_parts.append(f"\nSTDOUT:\n{result.stdout}")
            if result.error and result.error.traceback:
                output_parts.append(f"\nTRACEBACK:\n{result.error.traceback}")
            if result.error and result.error.message:
                output_parts.append(f"\nERROR: {result.error.message}")

            return "\n".join(output_parts)
        except Exception as e:
            return f"ERROR: Could not run tests: {e}"

    # ── Context building ──────────────────────────────────────────────────────

    def _build_initial_context(
        self,
        error_output: str,
        source_files: Dict[str, str],
        test_files: Dict[str, str],
        architecture_context: str,
        repair_history: List[str],
    ) -> str:
        """Build the initial context message for the debugger.

        Sends only file paths, a filtered file tree, and error output.
        The debugger is expected to use view_file / search_files to inspect
        file contents on demand.
        """
        parts = []

        # Filtered file tree — exclude noisy directories, capped
        parts.append("## Workspace File Tree")
        parts.append(build_filtered_file_tree(self._workspace))

        # Architecture context — do NOT include the blob; point to tools instead
        parts.append("\n## Architecture Context")
        parts.append(
            "Architecture plan is available via: view_file('.bizniz/architecture.md') "
            "if it exists, or use search_files to find architectural patterns."
        )

        # Source file paths only (no content)
        parts.append("\n## Source Files Under Test")
        if source_files:
            source_paths = list(source_files.keys())
            parts.append(f"Source files (use view_file to inspect): {', '.join(source_paths)}")
        else:
            parts.append("(none provided)")

        # Test file paths only (no content)
        parts.append("\n## Test Files")
        if test_files:
            test_paths = list(test_files.keys())
            parts.append(f"Test files (use view_file to inspect): {', '.join(test_paths)}")
        else:
            parts.append("(none provided)")

        # Error output — truncate to 4000 chars (first 2000 + last 2000)
        parts.append("\n## Error Output")
        if len(error_output) > 4000:
            parts.append(error_output[:2000])
            parts.append(f"\n... ({len(error_output) - 4000} chars truncated) ...")
            parts.append(error_output[-2000:])
        else:
            parts.append(error_output)

        # Repair history (kept as-is — it's small)
        if repair_history:
            parts.append("\n## Previous Repair Attempts")
            for i, entry in enumerate(repair_history, 1):
                parts.append(f"{i}. {entry}")

        parts.append(
            "\n## Instructions\n"
            "Diagnose this test failure. Use view_file to read the source and test files "
            "listed above. Use search_files and list_directory to explore the project structure. "
            "When you understand the root cause, use submit_fix with your diagnosis "
            "and optionally include direct code_fixes."
        )

        return "\n".join(parts)

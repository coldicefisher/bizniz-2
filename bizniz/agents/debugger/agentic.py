"""
AgenticDebugger — iterative tool-use debugging agent.

Uses view_file, list_directory, search_files, run_command, and run_tests
to explore the codebase and diagnose test failures. Optionally produces
direct code fixes.
"""

import json
import subprocess
import time
from typing import Optional, Callable, List, Dict

from bizniz.core.client import BaseAIClient
from bizniz.core.types import ResponseFormat
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

from bizniz.agents.debugger.base import BaseDebugger
from bizniz.agents.debugger.types import (
    AgenticDiagnosis,
    CodeFix,
    AgenticDebuggerError,
    AgenticDebuggerTimeoutError,
    AgenticDebuggerGaveUpError,
    AgenticDebuggerBadResponseError,
)
from bizniz.agents.debugger.prompts.agentic_system_prompt import AGENTIC_DEBUGGER_SYSTEM_PROMPT
from bizniz.agents.debugger.prompts.agentic_schema import AgenticDebuggerActionSchema


class AgenticDebugger(BaseDebugger):
    """
    Agentic debugging agent with tool-use capabilities.

    Parameters
    ----------
    client:
        AI client instance. Should be a dedicated instance (not shared with
        coder/tester) to avoid message history contamination.
    workspace:
        The workspace to explore files in.
    environment:
        Execution environment for running tests.
    tool_iterations:
        Maximum number of tool-call iterations before forcing a diagnosis.
        Each iteration is one LLM round-trip that may include a tool call
        (view_file, run_command, inspect_container, etc.).
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
        tool_iterations: int = 15,
        timeout_seconds: int = 1800,
        on_status_message: Optional[Callable[[str], None]] = None,
        compose_path: Optional[str] = None,
        service_name: Optional[str] = None,
    ):
        super().__init__(
            client=client,
            workspace=workspace,
            environment=environment,
            on_status_message=on_status_message,
        )
        self._tool_iterations = tool_iterations
        self._timeout_seconds = timeout_seconds
        self._compose_path = compose_path
        self._service_name = service_name

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

        self._log("AgenticDebugger: starting diagnosis...")

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

        for turn in range(1, self._tool_iterations + 1):
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed > self._timeout_seconds:
                self._log(f"AgenticDebugger: timeout after {int(elapsed)}s — forcing diagnosis")
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
                text, _, _ = self._ai_client.get_text(
                    messages=messages,
                    use_message_history=False,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=AgenticDebuggerActionSchema,
                )
            except Exception as e:
                self._log(f"AgenticDebugger: LLM call failed ({type(e).__name__}: {e})")
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
                self._log(f"AgenticDebugger: failed to parse response ({e})")
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
                self._log(f"AgenticDebugger: diagnosis submitted — {action.get('root_cause_category', 'unknown')} "
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
                self._log(f"AgenticDebugger: viewing {path}")
                result = self._tool_view_file(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: view_file(\"{path}\")]\n{result}",
                })

            elif action_type == "list_directory":
                self._log(f"AgenticDebugger: listing {path or '.'}")
                result = self._tool_list_directory(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: list_directory(\"{path}\")]\n{result}",
                })

            elif action_type == "search_files":
                self._log(f"AgenticDebugger: searching for '{path}'")
                result = self._tool_search_files(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: search_files(\"{path}\")]\n{result}",
                })

            elif action_type == "search_imports":
                self._log(f"AgenticDebugger: searching imports for '{path}'")
                from bizniz.tools.discovery_tools import tool_search_imports
                result = tool_search_imports(self._workspace, path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: search_imports(\"{path}\")]\n{result}",
                })

            elif action_type == "list_all_imports":
                self._log(f"AgenticDebugger: listing imports from '{path}'")
                from bizniz.tools.discovery_tools import tool_list_all_imports
                result = tool_list_all_imports(self._workspace, path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: list_all_imports(\"{path}\")]\n{result}",
                })

            elif action_type == "run_command":
                self._log(f"AgenticDebugger: running command: {path[:80]}")
                result = self._tool_run_command(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: run_command(\"{path}\")]\n{result}",
                })

            elif action_type == "run_tests":
                self._log(f"AgenticDebugger: running tests {path}")
                result = self._tool_run_tests(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: run_tests(\"{path}\")]\n{result}",
                })

            elif action_type == "inspect_container":
                self._log(f"AgenticDebugger: inspecting container logs ({path or 'default'})")
                result = self._tool_inspect_container(path)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: inspect_container(\"{path}\")]\n{result}",
                })

            elif action_type == "tail_logs":
                service = (action.get("service") or "").strip()
                lines = path.strip() or "100"
                self._log(f"AgenticDebugger: tailing logs ({service or 'self'}, {lines} lines)")
                result = self._tool_tail_logs(service, lines)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: tail_logs(service={service!r}, lines={lines!r})]\n{result}",
                })

            elif action_type == "run_in_container":
                service = (action.get("service") or "").strip()
                command = action.get("command") or ""
                self._log(f"AgenticDebugger: running in container ({service or 'self'}): {command[:80]}")
                result = self._tool_run_in_container(service, command)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: run_in_container(service={service!r}, command={command!r})]\n{result}",
                })

            elif action_type == "run_python_in_container":
                service = (action.get("service") or "").strip()
                command = action.get("command") or ""
                self._log(f"AgenticDebugger: running python in container ({service or 'self'}): {command[:80]}")
                result = self._tool_run_python_in_container(service, command)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: run_python_in_container(service={service!r})]\n{result}",
                })

            elif action_type == "hit_endpoint":
                service = (action.get("service") or "").strip()
                url = action.get("url") or ""
                request_data = action.get("request_data") or "{}"
                self._log(f"AgenticDebugger: hitting endpoint {url}")
                result = self._tool_hit_endpoint(service, url, request_data)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: hit_endpoint(url={url!r})]\n{result}",
                })

            elif action_type == "inspect_env":
                service = (action.get("service") or "").strip()
                prefix = path.strip()
                self._log(f"AgenticDebugger: inspecting env in {service or 'self'} (prefix={prefix!r})")
                result = self._tool_inspect_env(service, prefix)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: inspect_env(service={service!r}, prefix={prefix!r})]\n{result}",
                })

            elif action_type == "query_database":
                service = (action.get("service") or "").strip()
                sql = action.get("command") or ""
                self._log(f"AgenticDebugger: query_database ({service or 'auto'}): {sql[:120]}")
                result = self._tool_query_database(service, sql)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: query_database(service={service!r})]\n{result}",
                })

            elif action_type == "decode_jwt":
                token = action.get("token") or ""
                self._log("AgenticDebugger: decoding JWT")
                result = self._tool_decode_jwt(token)
                messages.append({
                    "role": "user",
                    "content": f"[TOOL RESULT: decode_jwt(token=<{len(token)} chars>)]\n{result}",
                })

            else:
                messages.append({
                    "role": "user",
                    "content": (
                        f"Unknown action '{action_type}'. Available: view_file, list_directory, "
                        f"search_files, search_imports, list_all_imports, run_command, run_tests, "
                        f"tail_logs, run_in_container, run_python_in_container, hit_endpoint, "
                        f"inspect_env, query_database, decode_jwt, submit_fix."
                    ),
                })

        # Exhausted turns without a diagnosis — force one
        self._log("AgenticDebugger: max turns reached — forcing diagnosis")
        messages.append({
            "role": "user",
            "content": (
                "You have used all available turns. You MUST submit your diagnosis now. "
                "Use action 'submit_fix' with your best diagnosis."
            ),
        })

        # One final call
        try:
            text, _, _ = self._ai_client.get_text(
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

    # -- Tool implementations --------------------------------------------------

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
            if len(output) > 10000:
                output = output[:10000] + "\n\n... (output truncated)"
            return output
        except subprocess.TimeoutExpired:
            return "ERROR: Command timed out after 60 seconds."
        except Exception as e:
            return f"ERROR: Command failed: {e}"

    def _tool_inspect_container(self, path: str) -> str:
        """Pull logs from the Docker container running this service.

        ``path`` controls what to fetch:
          - "" or "logs"     → last 100 lines of container logs
          - "logs 200"       → last 200 lines
          - "exec <command>" → run a command inside the container
        """
        if not self._compose_path or not self._service_name:
            return "ERROR: Container inspection not available (no compose_path/service_name configured)."

        path = (path or "").strip()
        if not path or path == "logs":
            path = "logs 100"

        try:
            if path.startswith("logs"):
                parts = path.split()
                n_lines = int(parts[1]) if len(parts) > 1 else 100
                n_lines = min(n_lines, 500)  # cap to avoid context explosion
                result = subprocess.run(
                    ["docker", "compose", "-f", self._compose_path,
                     "logs", "--no-color", "--tail", str(n_lines), self._service_name],
                    capture_output=True, text=True, timeout=30,
                )
                output = (result.stdout or "") + (result.stderr or "")
                if not output.strip():
                    return f"(no logs available for {self._service_name})"
                return f"=== Container logs ({self._service_name}, last {n_lines} lines) ===\n{output}"

            elif path.startswith("exec "):
                command = path[5:].strip()
                if not command:
                    return "ERROR: No command provided. Usage: exec <command>"
                result = subprocess.run(
                    ["docker", "compose", "-f", self._compose_path,
                     "exec", "-T", self._service_name, "sh", "-c", command],
                    capture_output=True, text=True, timeout=60,
                )
                output = (result.stdout or "") + (result.stderr or "")
                if len(output) > 10000:
                    output = output[:10000] + "\n\n... (output truncated)"
                return f"{output}\n(exit code: {result.returncode})"

            else:
                return f"ERROR: Unknown inspect_container subcommand '{path}'. Use 'logs', 'logs N', or 'exec <command>'."

        except subprocess.TimeoutExpired:
            return "ERROR: Container inspection timed out."
        except Exception as e:
            return f"ERROR: Container inspection failed: {e}"

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

    # -- Live container introspection --------------------------------------------

    def _resolve_service(self, service: str) -> Optional[str]:
        """Resolve the target container service name. Empty string falls back to
        the debugger's bound service. Returns None if no service is available."""
        return service or self._service_name

    def _exec_in_container(
        self,
        service: str,
        argv: List[str],
        timeout: int = 60,
    ) -> "subprocess.CompletedProcess":
        """Run a command inside the target container via docker compose exec."""
        cmd = [
            "docker", "compose", "-f", self._compose_path,
            "exec", "-T", service,
        ] + argv
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _tool_tail_logs(self, service: str, lines: str) -> str:
        if not self._compose_path:
            return "ERROR: tail_logs unavailable (no compose_path configured)."
        target = self._resolve_service(service)
        if not target:
            return "ERROR: tail_logs needs a service name (none was provided and the debugger isn't bound to one)."
        try:
            n = int(lines or "100")
        except ValueError:
            n = 100
        n = max(1, min(n, 500))
        try:
            r = subprocess.run(
                ["docker", "compose", "-f", self._compose_path,
                 "logs", "--no-color", "--tail", str(n), target],
                capture_output=True, text=True, timeout=30,
            )
            output = (r.stdout or "") + (r.stderr or "")
            if not output.strip():
                return f"(no logs available for {target})"
            return f"=== {target} (last {n} lines) ===\n{output}"
        except subprocess.TimeoutExpired:
            return "ERROR: tail_logs timed out."
        except Exception as e:
            return f"ERROR: tail_logs failed: {e}"

    def _tool_run_in_container(self, service: str, command: str) -> str:
        if not self._compose_path:
            return "ERROR: run_in_container unavailable (no compose_path configured)."
        if not command.strip():
            return "ERROR: run_in_container requires a non-empty `command`."
        target = self._resolve_service(service)
        if not target:
            return "ERROR: run_in_container needs a service name."
        try:
            r = self._exec_in_container(target, ["sh", "-c", command], timeout=60)
            output = (r.stdout or "") + (r.stderr or "")
            if len(output) > 10000:
                output = output[:10000] + "\n\n... (output truncated)"
            return f"{output}\n(exit code: {r.returncode})"
        except subprocess.TimeoutExpired:
            return "ERROR: run_in_container timed out (60s)."
        except Exception as e:
            return f"ERROR: run_in_container failed: {e}"

    def _tool_run_python_in_container(self, service: str, command: str) -> str:
        if not self._compose_path:
            return "ERROR: run_python_in_container unavailable (no compose_path configured)."
        if not command.strip():
            return "ERROR: run_python_in_container requires a non-empty `command` (Python code)."
        target = self._resolve_service(service)
        if not target:
            return "ERROR: run_python_in_container needs a service name."
        try:
            r = self._exec_in_container(target, ["python", "-c", command], timeout=60)
            output = (r.stdout or "") + (r.stderr or "")
            if len(output) > 10000:
                output = output[:10000] + "\n\n... (output truncated)"
            return f"{output}\n(exit code: {r.returncode})"
        except subprocess.TimeoutExpired:
            return "ERROR: run_python_in_container timed out (60s)."
        except Exception as e:
            return f"ERROR: run_python_in_container failed: {e}"

    def _tool_hit_endpoint(self, service: str, url: str, request_data: str) -> str:
        if not self._compose_path:
            return "ERROR: hit_endpoint unavailable (no compose_path configured)."
        if not url.strip():
            return "ERROR: hit_endpoint requires a `url`."
        target = self._resolve_service(service)
        if not target:
            return "ERROR: hit_endpoint needs a service name to issue the request from."

        # Parse the request_data JSON.
        import json as _json
        method = "GET"
        headers: Dict[str, str] = {}
        body = None
        try:
            data = _json.loads(request_data) if request_data and request_data.strip() else {}
            if isinstance(data, dict):
                method = (data.get("method") or "GET").upper()
                headers = data.get("headers") or {}
                body = data.get("body")
        except Exception as e:
            return f"ERROR: hit_endpoint could not parse request_data JSON: {e}"

        # Build curl command. Use --silent --show-error and -i for headers.
        argv = ["curl", "-sS", "-i", "--max-time", "30", "-X", method]
        for k, v in (headers or {}).items():
            argv.extend(["-H", f"{k}: {v}"])
        if body is not None:
            if isinstance(body, (dict, list)):
                argv.extend(["--data-binary", _json.dumps(body)])
                if "Content-Type" not in (headers or {}):
                    argv.extend(["-H", "Content-Type: application/json"])
            else:
                argv.extend(["--data-binary", str(body)])
        argv.append(url)

        try:
            r = self._exec_in_container(target, argv, timeout=45)
            output = (r.stdout or "") + (r.stderr or "")
            if len(output) > 10000:
                output = output[:10000] + "\n\n... (output truncated)"
            return f"{output}\n(curl exit code: {r.returncode})"
        except subprocess.TimeoutExpired:
            return "ERROR: hit_endpoint timed out (45s)."
        except FileNotFoundError:
            return "ERROR: curl not available in target container. Try a different service or use run_python_in_container with httpx."
        except Exception as e:
            return f"ERROR: hit_endpoint failed: {e}"

    def _tool_inspect_env(self, service: str, prefix: str) -> str:
        if not self._compose_path:
            return "ERROR: inspect_env unavailable (no compose_path configured)."
        target = self._resolve_service(service)
        if not target:
            return "ERROR: inspect_env needs a service name."
        try:
            r = self._exec_in_container(target, ["printenv"], timeout=15)
            if r.returncode != 0:
                return f"ERROR: printenv failed: {r.stderr or r.stdout}"
            lines = (r.stdout or "").splitlines()
            if prefix:
                lines = [ln for ln in lines if ln.startswith(prefix)]
            lines.sort()
            if not lines:
                hint = f" matching '{prefix}'" if prefix else ""
                return f"(no env vars{hint} in {target})"
            output = "\n".join(lines)
            if len(output) > 8000:
                output = output[:8000] + "\n\n... (truncated)"
            return f"=== env vars in {target}" + (f" (prefix='{prefix}')" if prefix else "") + " ===\n" + output
        except subprocess.TimeoutExpired:
            return "ERROR: inspect_env timed out."
        except Exception as e:
            return f"ERROR: inspect_env failed: {e}"

    def _tool_query_database(self, service: str, sql: str) -> str:
        if not self._compose_path:
            return "ERROR: query_database unavailable (no compose_path configured)."
        if not sql.strip():
            return "ERROR: query_database requires a SQL statement."

        target = service or self._guess_db_service()
        if not target:
            return "ERROR: query_database could not auto-detect a postgres service. Pass service= explicitly."

        # Use psql with env auth (DATABASE / POSTGRES_USER / POSTGRES_DB are
        # typically set on the postgres container by the provisioner template).
        # We launch psql with -At for unaligned, tuples-only output, and
        # enforce a per-statement timeout via --command.
        psql_argv = [
            "sh", "-c",
            f'psql -At -U "${{POSTGRES_USER:-dev}}" -d "${{POSTGRES_DB:-postgres}}" -c {self._shell_quote(sql)}',
        ]
        try:
            r = self._exec_in_container(target, psql_argv, timeout=30)
            output = (r.stdout or "") + (r.stderr or "")
            if len(output) > 10000:
                output = output[:10000] + "\n\n... (output truncated)"
            return f"=== psql {target} ===\n{output}\n(exit code: {r.returncode})"
        except subprocess.TimeoutExpired:
            return "ERROR: query_database timed out (30s)."
        except Exception as e:
            return f"ERROR: query_database failed: {e}"

    def _tool_decode_jwt(self, token: str) -> str:
        token = (token or "").strip()
        if not token:
            return "ERROR: decode_jwt requires a non-empty `token`."
        # Strip a possible "Bearer " prefix.
        if token.lower().startswith("bearer "):
            token = token.split(None, 1)[1]
        parts = token.split(".")
        if len(parts) != 3:
            return f"ERROR: token does not look like a JWT (expected 3 parts, got {len(parts)})."
        import base64 as _b64
        import json as _json
        try:
            def _decode(seg: str) -> dict:
                pad = seg + "=" * (-len(seg) % 4)
                return _json.loads(_b64.urlsafe_b64decode(pad.encode()))
            header = _decode(parts[0])
            payload = _decode(parts[1])
        except Exception as e:
            return f"ERROR: could not decode JWT: {e}"
        return (
            "=== JWT (signature NOT verified) ===\n"
            f"Header:\n{_json.dumps(header, indent=2)}\n\n"
            f"Payload:\n{_json.dumps(payload, indent=2)}"
        )

    @staticmethod
    def _shell_quote(s: str) -> str:
        """Single-quote-escape a string for shell."""
        return "'" + s.replace("'", "'\\''") + "'"

    def _guess_db_service(self) -> Optional[str]:
        """Best-effort auto-detect of the project's postgres service name by
        reading the compose file. Avoids a hardcoded 'database' assumption."""
        if not self._compose_path:
            return None
        try:
            import yaml
            with open(self._compose_path, "r") as f:
                compose = yaml.safe_load(f) or {}
            services = compose.get("services") or {}
            for name, spec in services.items():
                image = ((spec or {}).get("image") or "").lower()
                if "postgres" in image or "postgis" in image:
                    return name
        except Exception:
            return None
        return None

    # -- Context building -------------------------------------------------------

    def _build_initial_context(
        self,
        error_output: str,
        source_files: Dict[str, str],
        test_files: Dict[str, str],
        architecture_context: str,
        repair_history: List[str],
    ) -> str:
        """Build the initial context message for the debugger."""
        parts = []

        parts.append("## Workspace File Tree")
        parts.append(build_filtered_file_tree(self._workspace))

        parts.append("\n## Architecture Context")
        parts.append(
            "Architecture plan is available via: view_file('.bizniz/architecture.md') "
            "if it exists, or use search_files to find architectural patterns."
        )

        parts.append("\n## Source Files Under Test")
        if source_files:
            source_paths = list(source_files.keys())
            parts.append(f"Source files (use view_file to inspect): {', '.join(source_paths)}")
        else:
            parts.append("(none provided)")

        parts.append("\n## Test Files")
        if test_files:
            test_paths = list(test_files.keys())
            parts.append(f"Test files (use view_file to inspect): {', '.join(test_paths)}")
        else:
            parts.append("(none provided)")

        parts.append("\n## Error Output")
        if len(error_output) > 4000:
            parts.append(error_output[:2000])
            parts.append(f"\n... ({len(error_output) - 4000} chars truncated) ...")
            parts.append(error_output[-2000:])
        else:
            parts.append(error_output)

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

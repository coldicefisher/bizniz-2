import hashlib
import json
from typing import Optional, Callable, List

from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.errors import AIInsufficientFunds
from bizniz.autocoder.prompts.generate_prompts import GENERATE_SYSTEM_INSTRUCTIONS_PROMPT, GENERATE_RETURN_FORMAT_PROMPT
from bizniz.autocoder.prompts.repair_prompt import REPAIR_PROMPT
from bizniz.autocoder.prompts.generate_multi_prompt import (
    GENERATE_MULTI_SYSTEM_PROMPT,
    GENERATE_MULTI_USER_PROMPT_TEMPLATE,
    get_generate_multi_system_prompt,
)
from bizniz.autocoder.prompts.repair_multi_prompt import (
    REPAIR_MULTI_PROMPT_TEMPLATE,
    REPAIR_MULTI_SYSTEM_PROMPT,
)
from bizniz.autocoder.prompts.repair_inline_prompt import (
    REPAIR_INLINE_SYSTEM_PROMPT,
    REPAIR_INLINE_USER_PROMPT,
)
from bizniz.autocoder.prompts.prompt_schemas import GeneratePromptSchema, RepairPromptSchema
from bizniz.autocoder.prompts.tool_action_schema import (
    AutocoderGenerateActionSchema,
    AutocoderRepairActionSchema,
)
from bizniz.tools.tool_loop import run_tool_loop, ToolLoopError
from bizniz.autocoder.types import (
    AutocoderProcessError,
    AutocoderBadAIResponseError,
    AutocoderProcessResult,
    AutocoderOnEventCallback,
    FileChange,
)
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import ExecutionCallSpec
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.base_ai_agent import BaseAIAgent


class Autocoder(BaseAIAgent):
    '''
    Generates and repairs Python code until it executes error-free.

    Given a prompt, `process` produces code, runs it in the injected
    environment, and iteratively repairs it on failure. The final
    working code is persisted via the injected workspace.
    '''

    def __init__(
        self,
        client: BaseAIClient,
        environment: BaseExecutionEnvironment,
        workspace: BaseWorkspace,
        max_retries: Optional[int] = 5,
        on_event: Optional[Callable[[AutocoderOnEventCallback], None]] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(
            client=client,
            environment=environment,
            workspace=workspace,
            max_retries=max_retries,
            on_event=on_event,
            on_status_message=on_status_message,
        )

    # END CONSTRUCTOR ///////////////////////////////////////////////////////////////////////////

    @property
    def _process_system_prompt(self) -> str:
        return (
            GENERATE_SYSTEM_INSTRUCTIONS_PROMPT.format(
                evaluation_environment=self._environment.describe()
            )
            + GENERATE_RETURN_FORMAT_PROMPT
        )

    @staticmethod
    def _normalize_call_spec(data) -> dict:
        """Normalize call_spec from structured output (may be a JSON string or dict)."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        if isinstance(data.get("kwargs"), str):
            try:
                data["kwargs"] = json.loads(data["kwargs"])
            except (json.JSONDecodeError, TypeError):
                data["kwargs"] = {}
        # Ensure symbol is always present — default to "main" if the AI omitted it
        if not data.get("symbol"):
            data["symbol"] = "main"
        return data

    # Generate ///////////////////////////////////////////////////////////////////////////////////

    def generate_only(
        self,
        prompt: str,
        filename: str,
        on_status_message: Optional[Callable[[str], None]] = None,
    ) -> AutocoderProcessResult:
        '''
        Generate code for the given prompt and save it without executing.
        Use this when the caller has its own validation loop (e.g. orchestrator + pytest).
        '''
        if on_status_message is not None:
            self._on_status_message = on_status_message

        def log(message: str):
            if self._on_status_message is not None:
                self._on_status_message(message)

        log("Requesting code from AI...")
        try:
            code, call_spec = self._generate_code(
                messages=[Message(role="user", content=prompt)]
            )
        except Exception as e:
            raise AutocoderProcessError(f"Failed to generate code: {e}") from e

        log(f"Saving {filename} to workspace...")
        self._save_code_to_file(code=code, filename=filename, prompt=prompt)
        return AutocoderProcessResult(
            changes=[FileChange(filepath=filename, code=code, action="create")]
        )

    def generate(
        self,
        prompt: str,
        filename: str,
        on_event: Optional[Callable[[AutocoderOnEventCallback], None]] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
        on_save_code: Optional[Callable[[str], None]] = None,
    ) -> AutocoderProcessResult:
        '''
        Generate code for the given prompt, execute it, and repair any errors.
        Repeats until the code runs successfully or max_retries is exhausted.
        Saves the final working code to the workspace.
        '''

        if on_event is not None:
            self._on_event = on_event

        if on_status_message is not None:
            self._on_status_message = on_status_message

        def log(message: str):
            if self._on_status_message is not None:
                self._on_status_message(message)

        def return_result(original_code: str, output, new_code: str) -> AutocoderProcessResult:
            original_hash = hashlib.sha256(original_code.encode()).hexdigest() if original_code else None
            new_hash = hashlib.sha256(new_code.encode()).hexdigest() if new_code else None

            if not original_code or original_hash != new_hash:
                log("Saving new code to disk...")
                self._save_code_to_file(code=new_code, filename=filename, prompt=prompt)
                if on_save_code is not None:
                    log("Saving new code via on_save_code callback...")
                    on_save_code(new_code)
            else:
                log("Code unchanged, not saving.")

            return AutocoderProcessResult(
                changes=[FileChange(filepath=filename, code=new_code, action="create")],
                output=output,
            )

        # Load cached code if it exists
        cached_code = None
        if self._workspace.exists(path=filename):
            log(f"Loading existing code for {filename} from workspace...")
            cached_code = self._workspace.read_file(path=filename)

        # Generate initial code
        log("Requesting initial code from AI...")
        try:
            working_code, call_spec = self._generate_code(
                messages=[Message(role="user", content=prompt)]
            )
        except Exception as e:
            raise AutocoderProcessError(f"Failed to generate initial code: {e}") from e

        # Evaluation and repair loop
        for attempt in range(1, self.max_retries + 1):
            log(f"Evaluation attempt {attempt}/{self.max_retries}")

            evaluation = self._environment.execute(code=working_code, call_spec=call_spec)

            if not evaluation.success:
                error = evaluation.error
                if error is None:
                    raise AutocoderProcessError("Code evaluation failed without providing an error message.")
                log(f"Code execution failed: {str(error)}")
                working_code, call_spec = self._repair_code(
                    previous_code=working_code,
                    error_message=str(error),
                )
                continue

            log(f"Processing {filename} successful.")
            return return_result(
                original_code=cached_code,
                output=evaluation.result,
                new_code=working_code,
            )

        raise AutocoderProcessError(
            f"Unable to produce valid code for {filename} after {self.max_retries} attempts."
        )

    def repair(
        self,
        previous_code: str,
        error_message: str,
        filename: str,
    ) -> AutocoderProcessResult:
        """
        Public method for the orchestrator to request a code repair and save the result.
        Wraps _repair_code so callers don't touch the private method.
        """
        new_code, call_spec = self._repair_code(
            previous_code=previous_code,
            error_message=error_message,
        )
        self._save_code_to_file(code=new_code, filename=filename)
        return AutocoderProcessResult(
            changes=[FileChange(filepath=filename, code=new_code, action="modify")]
        )

    # Helpers ///////////////////////////////////////////////////////////////////////////////////

    def _generate_code(self, messages: List[Message]) -> tuple:
        '''
        Requests code from the AI and returns (code, call_spec).
        Retries up to 3 times on bad responses.
        '''
        attempts = 3
        last_error = None
        text = None

        self.add_messages_to_history(messages=messages)
        prompt_to_store = "\n".join([f"{m.role}: {m.content}" for m in messages])

        for attempt in range(1, attempts + 1):
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=GeneratePromptSchema,
                )
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    continue

                text = self.clean_llm_json(text)
                json_response = json.loads(text)

                code_raw = self._extract_code_from_response(json_response)
                if "\n" not in code_raw and "\\n" in code_raw:
                    code_raw = code_raw.replace("\\n", "\n")
                code = self._strip_code_block(code_raw)
                call_spec = ExecutionCallSpec(**self._normalize_call_spec(json_response.get("call_spec", {})))

                self.emit(AutocoderOnEventCallback(
                    stage="generate",
                    status="success",
                    code=code,
                    prompt=prompt_to_store,
                    response=text,
                    attempt=attempt,
                ))
                return code, call_spec

            except Exception as e:
                last_error = e
                continue

        self.emit(AutocoderOnEventCallback(
            stage="generate",
            status="failure",
            code=None,
            prompt=prompt_to_store,
            response=text,
            attempt=attempts,
        ))
        raise AutocoderBadAIResponseError(
            f"Assistant failed after multiple attempts.\nLast error: {last_error}"
        )

    def _repair_code(self, previous_code: str, error_message: str) -> tuple:
        '''
        Asks the AI to fix failing code and returns (code, call_spec).
        Retries up to 3 times on bad responses.
        '''
        attempts = 3
        last_error = None
        text = None

        if self._on_status_message is not None:
            self._on_status_message("Repairing code with error message: " + error_message)

        repair_prompt = REPAIR_PROMPT.format(
            error_message=error_message,
            previous_code=previous_code,
        )

        self.add_messages_to_history([{"role": "user", "content": repair_prompt}])

        for attempt in range(1, attempts + 1):
            new_code = None
            text = None
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=RepairPromptSchema,
                )
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI assistant"
                    continue

                if self._on_status_message is not None:
                    self._on_status_message(f"Repair attempt {attempt}: response received.")

                text = self.clean_llm_json(text)
                json_response = json.loads(text)

                code_raw = self._extract_code_from_response(json_response)
                if "\n" not in code_raw and "\\n" in code_raw:
                    code_raw = code_raw.replace("\\n", "\n")
                new_code = self._strip_code_block(code_raw)
                if not new_code or not new_code.strip():
                    last_error = "AI returned empty code during repair."
                    continue

                call_spec = ExecutionCallSpec(**self._normalize_call_spec(json_response.get("call_spec", {})))

                self.emit(AutocoderOnEventCallback(
                    stage="repair",
                    status="success",
                    code=new_code,
                    prompt=repair_prompt,
                    response=text,
                    attempt=attempt,
                ))
                return new_code, call_spec

            except AutocoderProcessError:
                self.emit(AutocoderOnEventCallback(
                    stage="repair",
                    status="failure",
                    code=new_code,
                    prompt=repair_prompt,
                    response=text,
                    attempt=attempt,
                ))
                raise

            except Exception as e:
                last_error = e
                self.emit(AutocoderOnEventCallback(
                    stage="repair",
                    status="failure",
                    code=new_code,
                    prompt=repair_prompt,
                    response=text,
                    attempt=attempt,
                ))
                continue

        self.emit(AutocoderOnEventCallback(
            stage="repair",
            status="failure",
            code=None,
            prompt=repair_prompt,
            response=text,
            attempt=attempts,
        ))
        raise AutocoderBadAIResponseError(
            f"Assistant failed to provide valid repaired code after multiple attempts.\nLast error: {last_error}"
        )

    @staticmethod
    def _extract_code_from_response(json_response: dict) -> str:
        """
        Extract code from AI response, handling both old format ({"code": "..."})
        and new multi-file format ({"changes": [{"code": "...", ...}]}).
        Returns the code string from the first file change, or the "code" field.
        """
        # New format: changes array
        changes = json_response.get("changes", [])
        if changes and isinstance(changes, list) and len(changes) > 0:
            return changes[0].get("code", "")
        # Old format: direct code field
        return json_response.get("code", "")

    # ── Multi-file API ────────────────────────────────────────────────────────

    def generate_multi(
        self,
        issue_description: str,
        target_files: List[dict],
        architecture_context: str = "",
        existing_code: Optional[dict] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
    ) -> AutocoderProcessResult:
        """
        Generate code across multiple files using agentic tool loop.

        The LLM discovers file contents via tools instead of receiving
        everything inline, keeping prompts small and token-efficient.
        """
        if on_status_message is not None:
            self._on_status_message = on_status_message

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Build target files description
        target_desc = "\n".join(
            f"- {tf['filepath']} ({tf.get('action', 'create')})"
            for tf in target_files
        )

        user_prompt = GENERATE_MULTI_USER_PROMPT_TEMPLATE.format(
            issue_description=issue_description,
            target_files_description=target_desc,
        )

        # Detect language from target files
        has_ts = any(
            tf["filepath"].endswith((".ts", ".tsx"))
            for tf in target_files
        )
        lang = "typescript" if has_ts else "python"

        system_prompt = get_generate_multi_system_prompt(lang).format(
            evaluation_environment=self._environment.describe(),
        )

        log(f"Requesting multi-file code generation ({len(target_files)} files)...")

        try:
            action = run_tool_loop(
                client=self._client,
                workspace=self._workspace,
                system_prompt=system_prompt,
                initial_user_message=user_prompt,
                action_schema=AutocoderGenerateActionSchema,
                terminal_action="submit_code",
                max_turns=6,
                timeout_seconds=300,
                on_status_message=self._on_status_message,
                agent_name="Autocoder",
            )
        except ToolLoopError as e:
            raise AutocoderBadAIResponseError(f"Tool loop failed: {e}")

        changes = self._parse_changes(action)
        if not changes:
            raise AutocoderBadAIResponseError("Tool loop returned no file changes")
        dependencies = action.get("dependencies", [])
        test_scaffold = action.get("test_scaffold", "")

        # Save all files to workspace
        for change in changes:
            log(f"Saving {change.filepath} to workspace...")
            self._workspace.write_file(path=change.filepath, content=change.code)

        if dependencies:
            log(f"Autocoder: LLM declared dependencies: {', '.join(dependencies)}")

        return AutocoderProcessResult(
            changes=changes, dependencies=dependencies, test_scaffold=test_scaffold,
        )

    def repair_multi(
        self,
        current_files: dict,
        error_message: str,
        architecture_context: str = "",
        on_status_message: Optional[Callable[[str], None]] = None,
    ) -> AutocoderProcessResult:
        """
        Repair code across multiple files using agentic tool loop.

        The LLM discovers file contents via tools instead of receiving
        everything inline, keeping prompts small and token-efficient.
        """
        if on_status_message is not None:
            self._on_status_message = on_status_message

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # List failing file paths (LLM reads them via tools)
        failing_files = "\n".join(f"- {fp}" for fp in current_files.keys())

        repair_prompt = REPAIR_MULTI_PROMPT_TEMPLATE.format(
            error_message=error_message,
            failing_files=failing_files,
        )

        log("Requesting multi-file repair...")

        try:
            action = run_tool_loop(
                client=self._client,
                workspace=self._workspace,
                system_prompt=REPAIR_MULTI_SYSTEM_PROMPT,
                initial_user_message=repair_prompt,
                action_schema=AutocoderRepairActionSchema,
                terminal_action="submit_code",
                max_turns=6,
                timeout_seconds=300,
                on_status_message=self._on_status_message,
                agent_name="Autocoder",
            )
        except ToolLoopError as e:
            raise AutocoderBadAIResponseError(f"Tool loop failed: {e}")

        changes = self._parse_changes(action)
        dependencies = action.get("dependencies", [])

        # Save all changed files to workspace
        for change in changes:
            log(f"Saving repaired {change.filepath} to workspace...")
            self._workspace.write_file(path=change.filepath, content=change.code)

        if dependencies:
            log(f"Autocoder: repair declared dependencies: {', '.join(dependencies)}")

        return AutocoderProcessResult(changes=changes, dependencies=dependencies)

    def repair_multi_inline(
        self,
        source_files: dict,
        test_files: dict,
        error_message: str,
        on_status_message: Optional[Callable[[str], None]] = None,
    ) -> AutocoderProcessResult:
        """
        Inline multi-file repair — no tool loop, all code sent inline.

        Two-shot: system+user → LLM returns analysis + changes in one call.
        Sends all relevant source and test file contents directly in the prompt
        so the LLM has full context without needing discovery tools.
        """
        if on_status_message is not None:
            self._on_status_message = on_status_message

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Format source files inline
        source_block = ""
        for fp, content in source_files.items():
            source_block += f"── {fp} ──\n```python\n{content}\n```\n\n"

        # Format test files inline
        test_block = ""
        for fp, content in test_files.items():
            test_block += f"── {fp} ──\n```python\n{content}\n```\n\n"

        user_prompt = REPAIR_INLINE_USER_PROMPT.format(
            error_output=error_message,
            source_files=source_block or "(no source files)",
            test_files=test_block or "(no test files)",
        )

        log(f"Autocoder: inline repair with {len(source_files)} source + {len(test_files)} test file(s)...")

        messages = [
            {"role": "system", "content": REPAIR_INLINE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        import time
        attempts = 3
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                t0 = time.time()
                text, job_id, output_messages = self._client.get_text(
                    messages=messages,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=RepairPromptSchema,
                )
                elapsed = time.time() - t0
                log(f"Autocoder: inline repair response in {elapsed:.1f}s (attempt {attempt})")

                if not text or not text.strip():
                    last_error = "Empty response"
                    continue

                text = self.clean_llm_json(text)
                json_response = json.loads(text)

                analysis = json_response.get("analysis", "")
                fix_plan = json_response.get("fix_plan", "")
                if analysis:
                    log(f"Autocoder: analysis — {analysis[:120]}")
                if fix_plan:
                    log(f"Autocoder: fix plan — {fix_plan[:120]}")

                changes = self._parse_changes(json_response)
                dependencies = json_response.get("dependencies", [])

                if not changes:
                    last_error = "No changes in response"
                    continue

                # Save repaired files
                for change in changes:
                    log(f"Saving repaired {change.filepath} to workspace...")
                    self._workspace.write_file(path=change.filepath, content=change.code)

                if dependencies:
                    log(f"Autocoder: repair declared dependencies: {', '.join(dependencies)}")

                return AutocoderProcessResult(changes=changes, dependencies=dependencies)

            except json.JSONDecodeError as e:
                last_error = f"JSON parse error: {e}"
                log(f"Autocoder: inline repair JSON error (attempt {attempt}): {e}")
            except AIInsufficientFunds:
                raise
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                log(f"Autocoder: inline repair error (attempt {attempt}): {e}")

        raise AutocoderBadAIResponseError(f"Inline repair failed after {attempts} attempts: {last_error}")

    # ── Multi-file private helpers ────────────────────────────────────────────

    def _generate_multi_code(self, user_prompt: str) -> tuple:
        """
        Send a multi-file generation prompt and return (list of FileChange, list of dependency strings).
        """
        import time
        attempts = 3
        last_error = None
        text = None

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                log(f"Autocoder: generate_multi AI call (attempt {attempt}/{attempts})...")
                t0 = time.time()
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=GeneratePromptSchema,
                )
                elapsed = time.time() - t0
                log(f"Autocoder: generate_multi AI responded in {elapsed:.1f}s ({len(text or '')} chars)")
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    log(f"Autocoder: empty response on attempt {attempt}")
                    continue

                text = self.clean_llm_json(text)
                json_response = json.loads(text)

                changes = self._parse_changes(json_response)
                if not changes:
                    last_error = "AI returned no file changes"
                    log(f"Autocoder: no changes in response on attempt {attempt}")
                    continue

                dependencies = json_response.get("dependencies", [])
                return changes, dependencies

            except AutocoderBadAIResponseError:
                raise
            except AIInsufficientFunds:
                raise
            except Exception as e:
                last_error = e
                log(f"Autocoder: generate attempt {attempt} failed — {type(e).__name__}: {str(e)[:200]}")
                if "Expecting" in str(e) or "json" in str(e).lower():
                    log("Autocoder: clearing message history due to parse error (prevent token bloat)")
                    self.clear_message_history()
                    self.add_messages_to_history([Message(role="user", content=user_prompt)])
                continue

        raise AutocoderBadAIResponseError(
            f"AI failed to produce multi-file code after {attempts} attempts. "
            f"Last error: {last_error}"
        )

    def _repair_multi_code(self, repair_prompt: str) -> tuple:
        """
        Send a multi-file repair prompt and return (list of FileChange, list of dependency strings).
        """
        import time
        attempts = 3
        last_error = None
        text = None

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        self.add_messages_to_history([Message(role="user", content=repair_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                log(f"Autocoder: repair_multi AI call (attempt {attempt}/{attempts})...")
                t0 = time.time()
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=RepairPromptSchema,
                )
                elapsed = time.time() - t0
                log(f"Autocoder: repair_multi AI responded in {elapsed:.1f}s ({len(text or '')} chars)")
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    log(f"Autocoder: empty repair response on attempt {attempt}")
                    continue

                text = self.clean_llm_json(text)
                json_response = json.loads(text)

                changes = self._parse_changes(json_response)
                if not changes:
                    last_error = "AI returned no file changes during repair"
                    log(f"Autocoder: no changes in repair response on attempt {attempt}")
                    continue

                dependencies = json_response.get("dependencies", [])
                return changes, dependencies

            except AutocoderBadAIResponseError:
                raise
            except AIInsufficientFunds:
                raise
            except Exception as e:
                last_error = e
                log(f"Autocoder: repair attempt {attempt} failed — {type(e).__name__}: {str(e)[:200]}")
                # Clear history on JSON parse failure to prevent token bloat on retry
                if "Expecting" in str(e) or "json" in str(e).lower():
                    log("Autocoder: clearing message history due to parse error (prevent token bloat)")
                    self.clear_message_history()
                    self.add_messages_to_history([Message(role="user", content=repair_prompt)])
                continue

        raise AutocoderBadAIResponseError(
            f"AI failed to produce multi-file repair after {attempts} attempts. "
            f"Last error: {last_error}"
        )

    def _parse_changes(self, json_response: dict) -> List[FileChange]:
        """Parse a changes array from AI response into FileChange objects."""
        raw_changes = json_response.get("changes", [])
        changes = []
        for ch in raw_changes:
            code_raw = ch.get("code", "")
            if "\n" not in code_raw and "\\n" in code_raw:
                code_raw = code_raw.replace("\\n", "\n")
            code = self._strip_code_block(code_raw)
            if code and code.strip():
                changes.append(FileChange(
                    filepath=ch["filepath"],
                    code=code,
                    action=ch.get("action", "create"),
                ))
        return changes

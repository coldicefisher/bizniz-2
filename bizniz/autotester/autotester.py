import json
from typing import Optional, Callable, List

from bizniz.base_ai_agent import BaseAIAgent
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.utils.code_metadata import read_code_metadata

from bizniz.autotester.types import (
    AutotesterResult,
    AutotesterOnEventCallback,
    AutotesterError,
    AutotesterBadAIResponseError,
    GeneratedTestFile,
)
from bizniz.autotester.prompts.system_prompt import AUTOTESTER_SYSTEM_PROMPT
from bizniz.autotester.prompts.from_code_prompt import FROM_CODE_PROMPT_TEMPLATE
from bizniz.autotester.prompts.from_prompt_prompt import FROM_PROMPT_PROMPT_TEMPLATE
from bizniz.autotester.prompts.review_prompt import REVIEW_PROMPT_TEMPLATE
from bizniz.autotester.prompts.generate_multi_prompt import GENERATE_MULTI_SYSTEM_PROMPT, GENERATE_MULTI_USER_PROMPT_TEMPLATE
from bizniz.autotester.prompts.schema import AutotesterSchema


class Autotester(BaseAIAgent):
    """
    AI agent that generates and reviews pytest test suites.

    Three modes
    -----------
    process_from_code(code_path, output_path)
        Mode 1 — reads existing code + its embedded problem statement,
        then asks the AI to write tests for it.

    process_from_prompt(prompt, output_path)
        Mode 2 — given a problem statement only (no code yet), asks the AI
        to write contract tests that any correct implementation must pass.

    review_tests(code_path, test_path, output_path)
        Mode 3 — reads existing code + existing tests, asks the AI to
        strengthen the tests with additional edge cases and better assertions.
    """

    def __init__(
        self,
        client: BaseAIClient,
        environment: BaseExecutionEnvironment,
        workspace: BaseWorkspace,
        max_retries: Optional[int] = 5,
        on_event: Optional[Callable[[AutotesterOnEventCallback], None]] = None,
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
        return AUTOTESTER_SYSTEM_PROMPT

    # ── Public API ─────────────────────────────────────────────────────────────

    def _lookup_problem_statement(self, code_path: str) -> Optional[str]:
        """
        Look up the problem statement for a code file from the workspace DB.
        Returns None if no matching issue exists.
        """
        try:
            ctx = self._workspace.db.get_context_for_code_file(code_path)
            if ctx and ctx.get("problem_statement"):
                return ctx["problem_statement"]
        except Exception:
            pass
        return None

    def process_from_code(
        self,
        code_path: str,
        output_path: str,
        on_event: Optional[Callable[[AutotesterOnEventCallback], None]] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
        on_save_tests: Optional[Callable[[str], None]] = None,
    ) -> AutotesterResult:
        """
        Mode 1: read code from workspace, look up its problem statement from the
        workspace DB (falling back to embedded file metadata), then generate a
        pytest test suite.

        Parameters
        ----------
        code_path:
            Workspace-relative path to the code file under test.
        output_path:
            Workspace-relative path where the generated test file will be saved.
        """
        self._update_callbacks(on_event, on_status_message)

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        log(f"Mode 1: reading code from {code_path}")
        code = self._workspace.read_file(path=code_path)

        # Source of truth: workspace DB first, then file metadata fallback
        problem_statement = self._lookup_problem_statement(code_path)
        if not problem_statement:
            metadata = read_code_metadata(code)
            problem_statement = metadata.get("problem_statement") or "(no problem statement found)"

        module_name = code_path.replace(".py", "")

        user_prompt = FROM_CODE_PROMPT_TEMPLATE.format(
            problem_statement=problem_statement,
            code=code,
            module_name=module_name,
        )

        log("Requesting tests from AI (from_code mode)...")
        tests = self._generate_tests(user_prompt, mode="from_code")

        self._save_tests(tests=tests, output_path=output_path, on_save_tests=on_save_tests)
        log(f"Tests saved to {output_path}")

        return AutotesterResult(
            test_files=[GeneratedTestFile(filepath=output_path, tests=tests)],
            mode="from_code",
            success=True,
        )

    def process_from_prompt(
        self,
        prompt: str,
        output_path: str,
        code_filename: Optional[str] = None,
        on_event: Optional[Callable[[AutotesterOnEventCallback], None]] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
        on_save_tests: Optional[Callable[[str], None]] = None,
    ) -> AutotesterResult:
        """
        Mode 2: given a problem statement only, generate contract tests that a
        correct implementation must pass.

        Parameters
        ----------
        prompt:
            The problem statement / feature description.
        output_path:
            Workspace-relative path where the test file will be saved.
        code_filename:
            Optional workspace-relative filename of the code module (e.g. "roman_to_int.py").
            Used to generate the correct import statement in tests.
        """
        self._update_callbacks(on_event, on_status_message)

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Derive module name from code_filename (strip .py extension)
        module_name = code_filename.replace(".py", "") if code_filename else "solution"

        user_prompt = FROM_PROMPT_PROMPT_TEMPLATE.format(
            problem_statement=prompt,
            module_name=module_name,
        )

        log("Requesting tests from AI (from_prompt mode)...")
        tests = self._generate_tests(user_prompt, mode="from_prompt")

        self._save_tests(tests=tests, output_path=output_path, on_save_tests=on_save_tests)
        log(f"Tests saved to {output_path}")

        return AutotesterResult(
            test_files=[GeneratedTestFile(filepath=output_path, tests=tests)],
            mode="from_prompt",
            success=True,
        )

    def review_tests(
        self,
        code_path: str,
        test_path: str,
        output_path: str,
        on_event: Optional[Callable[[AutotesterOnEventCallback], None]] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
        on_save_tests: Optional[Callable[[str], None]] = None,
    ) -> AutotesterResult:
        """
        Mode 3: read existing code + existing tests, strengthen the tests with
        additional edge cases and improved assertions.

        Parameters
        ----------
        code_path:
            Workspace-relative path to the code file under test.
        test_path:
            Workspace-relative path to the existing test file.
        output_path:
            Workspace-relative path where the improved test file will be saved
            (can be the same as test_path to overwrite in place).
        """
        self._update_callbacks(on_event, on_status_message)

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        log(f"Mode 3: reading code from {code_path} and tests from {test_path}")
        code = self._workspace.read_file(path=code_path)
        existing_tests = self._workspace.read_file(path=test_path)

        # Source of truth: workspace DB first, then file metadata fallback
        problem_statement = self._lookup_problem_statement(code_path)
        if not problem_statement:
            metadata = read_code_metadata(code)
            problem_statement = metadata.get("problem_statement") or "(no problem statement found)"

        module_name = code_path.replace(".py", "")

        user_prompt = REVIEW_PROMPT_TEMPLATE.format(
            problem_statement=problem_statement,
            code=code,
            existing_tests=existing_tests,
            module_name=module_name,
        )

        log("Requesting improved tests from AI (review mode)...")
        tests = self._generate_tests(user_prompt, mode="review")

        self._save_tests(tests=tests, output_path=output_path, on_save_tests=on_save_tests)
        log(f"Improved tests saved to {output_path}")

        return AutotesterResult(
            test_files=[GeneratedTestFile(filepath=output_path, tests=tests)],
            mode="review",
            success=True,
        )

    # ── Multi-file API ─────────────────────────────────────────────────────────

    def generate_multi(
        self,
        problem_statement: str,
        test_files: List[str],
        source_code: Optional[dict] = None,
        architecture_context: str = "",
        on_event: Optional[Callable[[AutotesterOnEventCallback], None]] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
    ) -> AutotesterResult:
        """
        Generate test suites across multiple test files for a multi-file project.

        Parameters
        ----------
        problem_statement:
            The issue/task description.
        test_files:
            List of test file paths to generate (e.g. ["tests/test_models.py", "tests/test_cli.py"]).
        source_code:
            Dict mapping filepath → source code content for the modules under test.
        architecture_context:
            Formatted string describing the architecture plan.
        """
        self._update_callbacks(on_event, on_status_message)

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        source_code = source_code or {}

        # Build source code section
        source_parts = []
        for fp, content in source_code.items():
            source_parts.append(f"── {fp} ──\n{content}")
        source_str = "\n\n".join(source_parts) if source_parts else "(no source code provided)"

        # Build test files description
        test_desc = "\n".join(f"- {tf}" for tf in test_files)

        user_prompt = GENERATE_MULTI_USER_PROMPT_TEMPLATE.format(
            problem_statement=problem_statement,
            architecture_context=architecture_context or "(none)",
            source_code=source_str,
            test_files_description=test_desc,
        )

        log(f"Requesting multi-file test generation ({len(test_files)} test files)...")
        result_files = self._generate_multi_tests(user_prompt)

        # Save all test files
        for tf in result_files:
            log(f"Saving tests to {tf.filepath}...")
            self._workspace.write_file(path=tf.filepath, content=tf.tests)

        return AutotesterResult(
            test_files=result_files,
            mode="from_prompt",
            success=True,
        )

    def _generate_multi_tests(self, user_prompt: str) -> List[GeneratedTestFile]:
        """
        Send a multi-file test generation prompt and return a list of GeneratedTestFile objects.
        """
        attempts = 3
        last_error = None
        text = None

        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=AutotesterSchema,
                )
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    continue

                text = self.clean_llm_json(text)
                json_response = json.loads(text)

                test_files_raw = json_response.get("test_files", [])
                if not test_files_raw:
                    last_error = "AI returned no test files"
                    continue

                result = []
                for tf_raw in test_files_raw:
                    tests_raw = tf_raw.get("tests", "")
                    if "\n" not in tests_raw and "\\n" in tests_raw:
                        tests_raw = tests_raw.replace("\\n", "\n")
                    tests = self._strip_code_block(tests_raw)
                    if tests and tests.strip():
                        result.append(GeneratedTestFile(
                            filepath=tf_raw["filepath"],
                            tests=tests,
                        ))

                if not result:
                    last_error = "AI returned empty test files"
                    continue

                return result

            except Exception as e:
                last_error = e
                continue

        raise AutotesterBadAIResponseError(
            f"AI failed to produce multi-file tests after {attempts} attempts. "
            f"Last error: {last_error}"
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _generate_tests(self, user_prompt: str, mode: str) -> str:
        """
        Send user_prompt to the AI and return the extracted test code string.
        Retries up to 3 times on bad/empty responses.
        """
        attempts = 3
        last_error = None
        text = None

        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=AutotesterSchema,
                )
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    continue

                text = self.clean_llm_json(text)
                json_response = json.loads(text)

                # Handle both formats: new "test_files" array or old "tests" string
                test_files_raw = json_response.get("test_files", [])
                if test_files_raw and isinstance(test_files_raw, list) and len(test_files_raw) > 0:
                    tests_raw = test_files_raw[0].get("tests", "")
                else:
                    tests_raw = json_response.get("tests", "")
                # Fix double-escaped newlines
                if "\n" not in tests_raw and "\\n" in tests_raw:
                    tests_raw = tests_raw.replace("\\n", "\n")
                tests = self._strip_code_block(tests_raw)
                if not tests or not tests.strip():
                    last_error = "AI returned empty test code"
                    continue

                self.emit(AutotesterOnEventCallback(
                    stage="generate",
                    status="success",
                    tests=tests,
                    prompt=user_prompt,
                    response=text,
                    attempt=attempt,
                ))
                return tests

            except Exception as e:
                last_error = e
                self.emit(AutotesterOnEventCallback(
                    stage="generate",
                    status="failure",
                    prompt=user_prompt,
                    response=text,
                    attempt=attempt,
                ))
                continue

        self.emit(AutotesterOnEventCallback(
            stage="generate",
            status="failure",
            prompt=user_prompt,
            response=text,
            attempt=attempts,
        ))
        raise AutotesterBadAIResponseError(
            f"AI failed to produce tests after {attempts} attempts. Last error: {last_error}"
        )

    def _save_tests(
        self,
        tests: str,
        output_path: str,
        on_save_tests: Optional[Callable[[str], None]] = None,
    ):
        """Write the test code to the workspace."""
        self._workspace.write_file(path=output_path, content=tests)
        self.emit(AutotesterOnEventCallback(
            stage="save",
            status="success",
            tests=tests,
        ))
        if on_save_tests is not None:
            on_save_tests(tests)

    def _update_callbacks(self, on_event, on_status_message):
        if on_event is not None:
            self._on_event = on_event
        if on_status_message is not None:
            self._on_status_message = on_status_message

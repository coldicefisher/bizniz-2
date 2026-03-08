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
)
from bizniz.autotester.prompts.system_prompt import AUTOTESTER_SYSTEM_PROMPT
from bizniz.autotester.prompts.from_code_prompt import FROM_CODE_PROMPT_TEMPLATE
from bizniz.autotester.prompts.from_prompt_prompt import FROM_PROMPT_PROMPT_TEMPLATE
from bizniz.autotester.prompts.review_prompt import REVIEW_PROMPT_TEMPLATE
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

    def process_from_code(
        self,
        code_path: str,
        output_path: str,
        on_event: Optional[Callable[[AutotesterOnEventCallback], None]] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
        on_save_tests: Optional[Callable[[str], None]] = None,
    ) -> AutotesterResult:
        """
        Mode 1: read code from workspace, read its embedded problem statement,
        generate a pytest test suite.

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
        metadata = read_code_metadata(code)
        problem_statement = metadata.get("problem_statement") or "(no problem statement found)"

        user_prompt = FROM_CODE_PROMPT_TEMPLATE.format(
            problem_statement=problem_statement,
            code=code,
        )

        log("Requesting tests from AI (from_code mode)...")
        tests = self._generate_tests(user_prompt, mode="from_code")

        self._save_tests(tests=tests, output_path=output_path, on_save_tests=on_save_tests)
        log(f"Tests saved to {output_path}")

        return AutotesterResult(
            tests=tests,
            output_path=output_path,
            mode="from_code",
            success=True,
        )

    def process_from_prompt(
        self,
        prompt: str,
        output_path: str,
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
        """
        self._update_callbacks(on_event, on_status_message)

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        user_prompt = FROM_PROMPT_PROMPT_TEMPLATE.format(problem_statement=prompt)

        log("Requesting tests from AI (from_prompt mode)...")
        tests = self._generate_tests(user_prompt, mode="from_prompt")

        self._save_tests(tests=tests, output_path=output_path, on_save_tests=on_save_tests)
        log(f"Tests saved to {output_path}")

        return AutotesterResult(
            tests=tests,
            output_path=output_path,
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
        metadata = read_code_metadata(code)
        problem_statement = metadata.get("problem_statement") or "(no problem statement found)"

        user_prompt = REVIEW_PROMPT_TEMPLATE.format(
            problem_statement=problem_statement,
            code=code,
            existing_tests=existing_tests,
        )

        log("Requesting improved tests from AI (review mode)...")
        tests = self._generate_tests(user_prompt, mode="review")

        self._save_tests(tests=tests, output_path=output_path, on_save_tests=on_save_tests)
        log(f"Improved tests saved to {output_path}")

        return AutotesterResult(
            tests=tests,
            output_path=output_path,
            mode="review",
            success=True,
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

                tests = self._strip_code_block(json_response.get("tests", ""))
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

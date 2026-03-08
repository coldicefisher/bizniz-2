import hashlib
import json
from typing import Optional, Callable, List

from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.autocoder.prompts.generate_prompts import GENERATE_SYSTEM_INSTRUCTIONS_PROMPT, GENERATE_RETURN_FORMAT_PROMPT
from bizniz.autocoder.prompts.repair_prompt import REPAIR_PROMPT
from bizniz.autocoder.prompts.prompt_schemas import GeneratePromptSchema, RepairPromptSchema
from bizniz.autocoder.types import (
    AutocoderProcessError,
    AutocoderBadAIResponseError,
    AutocoderProcessResult,
    AutocoderOnEventCallback,
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
        return AutocoderProcessResult(code=code, output=None)

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

            return AutocoderProcessResult(code=new_code, output=output)

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
        return AutocoderProcessResult(code=new_code, output=None)

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

                code = self._strip_code_block(json_response.get("code", ""))
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

                new_code = self._strip_code_block(json_response.get("code", ""))
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

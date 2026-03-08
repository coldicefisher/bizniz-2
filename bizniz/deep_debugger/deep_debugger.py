"""
DeepDebugger

A standalone debugging agent that spins up with a fresh LLM instance (no
shared message history) to perform comprehensive diagnosis when the repair
loop has stalled.

Unlike the regular Autodebugger which does quick per-iteration diagnosis,
the DeepDebugger receives ALL context — every source file, every test file,
full error output, architecture plan, and repair history — and produces a
structured fix plan.

Usage
-----
The orchestrator creates a DeepDebugger on demand when a stall is detected.
The DeepDebugger gets its own client instance so there is zero history
contamination from previous repair attempts.
"""

import json
from typing import Optional, Callable, List, Dict

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat

from bizniz.deep_debugger.types import (
    DeepDiagnosis,
    DeepDebuggerBadAIResponseError,
)
from bizniz.deep_debugger.prompts.deep_diagnose_prompt import (
    DEEP_DIAGNOSE_SYSTEM_PROMPT,
    DEEP_DIAGNOSE_PROMPT_TEMPLATE,
)
from bizniz.deep_debugger.prompts.schema import DeepDiagnosisSchema

from bizniz.utils.json import clean_llm_json


class DeepDebugger:
    """
    Standalone deep diagnosis agent with its own fresh AI client.

    Parameters
    ----------
    client:
        A dedicated AI client instance. Should NOT be the same instance
        shared by the autocoder/autotester — this ensures a clean context.
    on_status_message:
        Optional callback for human-readable status updates.
    """

    def __init__(
        self,
        client: BaseAIClient,
        on_status_message: Optional[Callable[[str], None]] = None,
    ):
        self._client = client
        self._on_status_message = on_status_message

    def diagnose(
        self,
        error_output: str,
        source_files: Dict[str, str],
        test_files: Dict[str, str],
        architecture_context: str = "",
        repair_history: List[str] = None,
    ) -> DeepDiagnosis:
        """
        Perform a comprehensive diagnosis with full project context.

        Parameters
        ----------
        error_output:
            The current test failure output (full pytest output).
        source_files:
            Dict mapping filepath to file content for all source files.
        test_files:
            Dict mapping filepath to file content for all test files.
        architecture_context:
            Formatted architecture plan string.
        repair_history:
            List of previous repair attempt summaries.

        Returns
        -------
        DeepDiagnosis with root_cause, fix_plan, affected_files, etc.
        """
        if repair_history is None:
            repair_history = []

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        log("DeepDebugger: analyzing full project context...")

        # Format source files
        source_parts = []
        for filepath, content in source_files.items():
            source_parts.append(f"--- {filepath} ---\n{content}\n")
        source_section = "\n".join(source_parts) if source_parts else "(no source files)"

        # Format test files
        test_parts = []
        for filepath, content in test_files.items():
            test_parts.append(f"--- {filepath} ---\n{content}\n")
        test_section = "\n".join(test_parts) if test_parts else "(no test files)"

        # Format repair history
        if repair_history:
            history_section = "\n".join(
                f"{i}. {entry}" for i, entry in enumerate(repair_history, 1)
            )
        else:
            history_section = "(no previous attempts)"

        # Build messages from scratch — completely fresh context
        messages = [
            {"role": "system", "content": DEEP_DIAGNOSE_SYSTEM_PROMPT},
            {"role": "user", "content": DEEP_DIAGNOSE_PROMPT_TEMPLATE.format(
                architecture_context=architecture_context or "(none provided)",
                source_files=source_section,
                test_files=test_section,
                error_output=error_output,
                repair_history=history_section,
            )},
        ]

        # Retry loop
        attempts = 3
        last_error = None
        text = None

        for attempt in range(1, attempts + 1):
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=messages,
                    use_message_history=False,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=DeepDiagnosisSchema,
                )

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    continue

                text = clean_llm_json(text)
                json_response = json.loads(text)

                diagnosis = DeepDiagnosis(
                    root_cause=json_response["root_cause"],
                    root_cause_category=json_response["root_cause_category"],
                    fix_target=json_response["fix_target"],
                    affected_files=json_response["affected_files"],
                    fix_plan=json_response["fix_plan"],
                    suggested_approach=json_response["suggested_approach"],
                    missing_packages=json_response.get("missing_packages", []),
                    confidence=json_response["confidence"],
                    repair_history_analysis=json_response["repair_history_analysis"],
                )

                log(f"DeepDebugger: root cause identified — {diagnosis.root_cause_category} "
                    f"(confidence: {diagnosis.confidence})")
                return diagnosis

            except Exception as e:
                last_error = e
                log(f"DeepDebugger: attempt {attempt} failed — {e}")
                continue

        raise DeepDebuggerBadAIResponseError(
            f"AI failed to produce deep diagnosis after {attempts} attempts. "
            f"Last error: {last_error}"
        )

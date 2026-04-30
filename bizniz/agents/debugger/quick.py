"""
QuickDebugger — one-shot diagnosis agent (no tools, fast, cheap).

Formerly known as QuickDebugger. Scans the workspace for related files and
asks the AI for a single-shot structured diagnosis.
"""

import json
import re
from typing import Optional, Callable, List, Dict

from bizniz.core.agent import BaseAIAgent
from bizniz.core.client import BaseAIClient
from bizniz.core.types import Message, ResponseFormat
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace

from bizniz.agents.debugger.base import BaseDebugger
from bizniz.agents.debugger.types import (
    QuickDebuggerDiagnosis,
    QuickDebuggerOnEventCallback,
    QuickDebuggerError,
    QuickDebuggerBadAIResponseError,
)
from bizniz.agents.debugger.prompts.quick_system_prompt import QUICK_DEBUGGER_SYSTEM_PROMPT
from bizniz.agents.debugger.prompts.quick_diagnose_prompt import DIAGNOSE_PROMPT_TEMPLATE
from bizniz.agents.debugger.prompts.quick_schema import QuickDebuggerSchema


class QuickDebugger(BaseDebugger, BaseAIAgent):
    """
    AI agent that diagnoses test failures by scanning the workspace for
    relevant files and producing a structured diagnosis.

    The diagnosis tells the orchestrator:
    - What the root cause is
    - Whether to fix the code or the tests
    - What related files provide needed context
    - How to approach the fix
    """

    def __init__(
        self,
        client: BaseAIClient,
        environment: BaseExecutionEnvironment,
        workspace: BaseWorkspace,
        max_retries: Optional[int] = 5,
        on_event: Optional[Callable[[QuickDebuggerOnEventCallback], None]] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
    ):
        BaseDebugger.__init__(
            self,
            client=client,
            workspace=workspace,
            environment=environment,
            on_status_message=on_status_message,
        )
        BaseAIAgent.__init__(
            self,
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
        return QUICK_DEBUGGER_SYSTEM_PROMPT

    # -- Public API -------------------------------------------------------------

    def diagnose(
        self,
        error_output: str,
        code: str,
        code_filename: str,
        test_code: str,
        test_filename: str,
        on_event: Optional[Callable[[QuickDebuggerOnEventCallback], None]] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
    ) -> QuickDebuggerDiagnosis:
        """
        Analyze a test failure and produce a structured diagnosis.

        Parameters
        ----------
        error_output:
            The full pytest error output (stdout + stderr + traceback).
        code:
            The source code of the module under test.
        code_filename:
            Workspace-relative filename of the code module.
        test_code:
            The source code of the failing test file.
        test_filename:
            Workspace-relative filename of the test file.

        Returns
        -------
        QuickDebuggerDiagnosis with diagnosis, fix_target, relevant_files, suggested_approach.
        """
        if on_event is not None:
            self._on_event = on_event
        if on_status_message is not None:
            self._on_status_message = on_status_message

        # Step 1: Scan workspace and find related files
        self._log("QuickDebugger: scanning workspace for related files...")
        self.emit(QuickDebuggerOnEventCallback(stage="scan", status="start"))

        # Filter out noisy directories from workspace listing
        _EXCLUDED_DIRS = {"node_modules", "__pycache__", ".git", ".bizniz", "dist", "build", ".next"}
        workspace_files = self._workspace.list_relative_files()
        filtered_files = [
            str(f) for f in workspace_files
            if not any(part in _EXCLUDED_DIRS for part in str(f).split("/"))
        ]
        workspace_listing = "\n".join(filtered_files[:30])

        # Find files referenced in error output or imports
        related = self._find_related_files(
            error_output=error_output,
            code=code,
            test_code=test_code,
            code_filename=code_filename,
            test_filename=test_filename,
            workspace_files=[str(f) for f in workspace_files],
        )

        self.emit(QuickDebuggerOnEventCallback(stage="scan", status="success"))

        # Step 2: Build the related files listing (paths only, no contents)
        related_listing = ""
        if related:
            related_listing = "\n".join(related)

        # Step 3: Truncate error output to cap token usage
        truncated_error = error_output
        if len(error_output) > 3000:
            truncated_error = error_output[:1500] + "\n\n... [truncated] ...\n\n" + error_output[-1500:]

        # Step 4: Ask the AI for a diagnosis
        self._log(f"QuickDebugger: diagnosing failure ({len(related)} related files found)...")
        user_prompt = DIAGNOSE_PROMPT_TEMPLATE.format(
            error_output=truncated_error,
            code=code,
            code_filename=code_filename,
            test_code=test_code,
            test_filename=test_filename,
            workspace_files=workspace_listing,
            related_files_listing=related_listing,
        )

        diagnosis = self._get_diagnosis(user_prompt)
        self._log(f"QuickDebugger: fix_target={diagnosis.fix_target}")
        return diagnosis

    # -- File scanning ----------------------------------------------------------

    def _find_related_files(
        self,
        error_output: str,
        code: str,
        test_code: str,
        code_filename: str,
        test_filename: str,
        workspace_files: List[str],
    ) -> List[str]:
        """
        Identify workspace files that are likely relevant to the failure.

        Recursively follows import chains so transitive dependencies are
        discovered.  Also picks up __init__.py files for any referenced
        packages and extracts full file paths from tracebacks.
        """
        workspace_set = set(workspace_files)
        related: set = set()
        visited_sources: set = set()

        def _resolve_module(module_dotpath: str) -> List[str]:
            hits = []
            as_path = module_dotpath.replace(".", "/")
            candidates = [
                as_path + ".py",
                as_path + "/__init__.py",
                module_dotpath + ".py",
            ]
            for c in candidates:
                if c in workspace_set:
                    hits.append(c)
            parts = as_path.split("/")
            for i in range(1, len(parts) + 1):
                init = "/".join(parts[:i]) + "/__init__.py"
                if init in workspace_set:
                    hits.append(init)
            return hits

        def _extract_imports(source: str) -> List[str]:
            modules = []
            for match in re.finditer(r'(?:from|import)\s+([\w.]+)', source):
                modules.append(match.group(1))
            return modules

        def _follow_imports(source: str, source_id: str):
            if source_id in visited_sources:
                return
            visited_sources.add(source_id)

            for module in _extract_imports(source):
                resolved = _resolve_module(module)
                for fpath in resolved:
                    if fpath not in related and fpath != code_filename and fpath != test_filename:
                        related.add(fpath)
                        try:
                            content = self._workspace.read_file(path=fpath)
                            if content:
                                _follow_imports(content, fpath)
                        except Exception:
                            pass

        _follow_imports(code, code_filename)
        _follow_imports(test_code, test_filename)

        for match in re.finditer(r'([\w./\\-]+\.py)', error_output):
            fname = match.group(1)
            if fname in workspace_set:
                related.add(fname)
            basename = fname.rsplit("/", 1)[-1] if "/" in fname else fname
            for wf in workspace_files:
                if wf.endswith("/" + basename) or wf == basename:
                    related.add(wf)

        init_files = set()
        for fpath in list(related):
            parts = fpath.split("/")
            for i in range(1, len(parts)):
                init = "/".join(parts[:i]) + "/__init__.py"
                if init in workspace_set:
                    init_files.add(init)
        related.update(init_files)

        related.discard(code_filename)
        related.discard(test_filename)

        return sorted(related)

    def _read_related_files(self, filenames: List[str]) -> Dict[str, str]:
        contents = {}
        for fname in filenames:
            try:
                content = self._workspace.read_file(path=fname)
                if content:
                    contents[fname] = content
            except Exception as e:
                self._log(f"QuickDebugger: could not read related file '{fname}': {e}")
                continue
        return contents

    # -- AI interaction ---------------------------------------------------------

    def _get_diagnosis(self, user_prompt: str) -> QuickDebuggerDiagnosis:
        attempts = 3
        last_error = None
        text = None

        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=QuickDebuggerSchema,
                )
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    continue

                text = self.clean_llm_json(text)
                json_response = json.loads(text)

                raw_files = json_response.get("relevant_files", [])
                if isinstance(raw_files, list):
                    relevant_files = {
                        entry["filename"]: entry["summary"]
                        for entry in raw_files
                        if isinstance(entry, dict) and "filename" in entry
                    }
                elif isinstance(raw_files, dict):
                    relevant_files = raw_files
                else:
                    relevant_files = {}

                diagnosis = QuickDebuggerDiagnosis(
                    diagnosis=json_response.get("diagnosis", ""),
                    fix_target=json_response.get("fix_target", "code"),
                    relevant_files=relevant_files,
                    suggested_approach=json_response.get("suggested_approach", ""),
                )

                self.emit(QuickDebuggerOnEventCallback(
                    stage="diagnose",
                    status="success",
                    diagnosis=diagnosis.diagnosis,
                    prompt=user_prompt,
                    response=text,
                    attempt=attempt,
                ))
                return diagnosis

            except Exception as e:
                last_error = e
                self.emit(QuickDebuggerOnEventCallback(
                    stage="diagnose",
                    status="failure",
                    prompt=user_prompt,
                    response=text,
                    attempt=attempt,
                ))
                continue

        self.emit(QuickDebuggerOnEventCallback(
            stage="diagnose",
            status="failure",
            prompt=user_prompt,
            response=text,
            attempt=attempts,
        ))
        raise QuickDebuggerBadAIResponseError(
            f"AI failed to produce diagnosis after {attempts} attempts. Last error: {last_error}"
        )


# Backward-compatible alias
QuickDebugger = QuickDebugger

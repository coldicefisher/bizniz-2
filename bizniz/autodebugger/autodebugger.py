import json
import re
from typing import Optional, Callable, List, Dict

from bizniz.base_ai_agent import BaseAIAgent
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace

from bizniz.autodebugger.types import (
    AutodebuggerDiagnosis,
    AutodebuggerOnEventCallback,
    AutodebuggerError,
    AutodebuggerBadAIResponseError,
)
from bizniz.autodebugger.prompts.system_prompt import AUTODEBUGGER_SYSTEM_PROMPT
from bizniz.autodebugger.prompts.diagnose_prompt import DIAGNOSE_PROMPT_TEMPLATE
from bizniz.autodebugger.prompts.schema import AutodebuggerSchema


class Autodebugger(BaseAIAgent):
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
        on_event: Optional[Callable[[AutodebuggerOnEventCallback], None]] = None,
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
        return AUTODEBUGGER_SYSTEM_PROMPT

    # ── Public API ─────────────────────────────────────────────────────────────

    def diagnose(
        self,
        error_output: str,
        code: str,
        code_filename: str,
        test_code: str,
        test_filename: str,
        on_event: Optional[Callable[[AutodebuggerOnEventCallback], None]] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
    ) -> AutodebuggerDiagnosis:
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
        AutodebuggerDiagnosis with diagnosis, fix_target, relevant_files, suggested_approach.
        """
        if on_event is not None:
            self._on_event = on_event
        if on_status_message is not None:
            self._on_status_message = on_status_message

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Step 1: Scan workspace and find related files
        log("Autodebugger: scanning workspace for related files...")
        self.emit(AutodebuggerOnEventCallback(stage="scan", status="start"))

        workspace_files = self._workspace.list_relative_files()
        workspace_listing = "\n".join(str(f) for f in workspace_files)

        # Find files referenced in error output or imports
        related = self._find_related_files(
            error_output=error_output,
            code=code,
            test_code=test_code,
            code_filename=code_filename,
            test_filename=test_filename,
            workspace_files=[str(f) for f in workspace_files],
        )

        related_contents = self._read_related_files(related)
        self.emit(AutodebuggerOnEventCallback(stage="scan", status="success"))

        # Step 2: Build the related file contents section
        related_section = ""
        if related_contents:
            parts = ["RELATED FILE CONTENTS:", "─" * 62]
            for fname, content in related_contents.items():
                parts.append(f"\n── {fname} ──")
                parts.append(content)
            related_section = "\n".join(parts)

        # Step 3: Ask the AI for a diagnosis
        log(f"Autodebugger: diagnosing failure ({len(related_contents)} related files found)...")
        user_prompt = DIAGNOSE_PROMPT_TEMPLATE.format(
            error_output=error_output,
            code=code,
            code_filename=code_filename,
            test_code=test_code,
            test_filename=test_filename,
            workspace_files=workspace_listing,
            related_file_contents=related_section,
        )

        diagnosis = self._get_diagnosis(user_prompt)
        log(f"Autodebugger: fix_target={diagnosis.fix_target}")
        return diagnosis

    # ── File scanning ──────────────────────────────────────────────────────────

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
        visited_sources: set = set()  # sources whose imports we already scanned

        # -- helpers ----------------------------------------------------------

        def _resolve_module(module_dotpath: str) -> List[str]:
            """Return workspace files that could correspond to a dotted module path."""
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
            # Also add __init__.py for every parent package
            parts = as_path.split("/")
            for i in range(1, len(parts) + 1):
                init = "/".join(parts[:i]) + "/__init__.py"
                if init in workspace_set:
                    hits.append(init)
            return hits

        def _extract_imports(source: str) -> List[str]:
            """Extract dotted module paths from import statements."""
            modules = []
            for match in re.finditer(r'(?:from|import)\s+([\w.]+)', source):
                modules.append(match.group(1))
            return modules

        def _follow_imports(source: str, source_id: str):
            """Recursively follow imports from *source* into workspace files."""
            if source_id in visited_sources:
                return
            visited_sources.add(source_id)

            for module in _extract_imports(source):
                resolved = _resolve_module(module)
                for fpath in resolved:
                    if fpath not in related and fpath != code_filename and fpath != test_filename:
                        related.add(fpath)
                        # Read the file and recurse into its imports
                        try:
                            content = self._workspace.read_file(path=fpath)
                            if content:
                                _follow_imports(content, fpath)
                        except Exception:
                            pass

        # -- 1. Recursively follow imports from code and tests ----------------
        _follow_imports(code, code_filename)
        _follow_imports(test_code, test_filename)

        # -- 2. Extract full file paths from traceback ------------------------
        # Matches paths like "path/to/module.py" or just "module.py"
        for match in re.finditer(r'([\w./\\-]+\.py)', error_output):
            fname = match.group(1)
            if fname in workspace_set:
                related.add(fname)
            # Also try just the basename
            basename = fname.rsplit("/", 1)[-1] if "/" in fname else fname
            for wf in workspace_files:
                if wf.endswith("/" + basename) or wf == basename:
                    related.add(wf)

        # -- 3. Discover __init__.py files for referenced packages ------------
        init_files = set()
        for fpath in list(related):
            parts = fpath.split("/")
            for i in range(1, len(parts)):
                init = "/".join(parts[:i]) + "/__init__.py"
                if init in workspace_set:
                    init_files.add(init)
        related.update(init_files)

        # Don't include the code or test file themselves — we already have those
        related.discard(code_filename)
        related.discard(test_filename)

        return sorted(related)

    def _read_related_files(self, filenames: List[str]) -> Dict[str, str]:
        """Read workspace files, returning a dict of filename → content."""
        contents = {}
        for fname in filenames:
            try:
                content = self._workspace.read_file(path=fname)
                if content:
                    contents[fname] = content
            except Exception as e:
                if self._on_status_message:
                    self._on_status_message(f"Autodebugger: could not read related file '{fname}': {e}")
                continue
        return contents

    # ── AI interaction ─────────────────────────────────────────────────────────

    def _get_diagnosis(self, user_prompt: str) -> AutodebuggerDiagnosis:
        """
        Send the diagnosis prompt to the AI and return a structured diagnosis.
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
                    schema=AutodebuggerSchema,
                )
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    continue

                text = self.clean_llm_json(text)
                json_response = json.loads(text)

                # Convert relevant_files from array format to dict
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

                diagnosis = AutodebuggerDiagnosis(
                    diagnosis=json_response.get("diagnosis", ""),
                    fix_target=json_response.get("fix_target", "code"),
                    relevant_files=relevant_files,
                    suggested_approach=json_response.get("suggested_approach", ""),
                )

                self.emit(AutodebuggerOnEventCallback(
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
                self.emit(AutodebuggerOnEventCallback(
                    stage="diagnose",
                    status="failure",
                    prompt=user_prompt,
                    response=text,
                    attempt=attempt,
                ))
                continue

        self.emit(AutodebuggerOnEventCallback(
            stage="diagnose",
            status="failure",
            prompt=user_prompt,
            response=text,
            attempt=attempts,
        ))
        raise AutodebuggerBadAIResponseError(
            f"AI failed to produce diagnosis after {attempts} attempts. Last error: {last_error}"
        )

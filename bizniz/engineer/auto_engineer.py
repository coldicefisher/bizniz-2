"""
AutoEngineer

Takes a problem statement, uses AI to produce structured engineering artifacts
(business requirements, use cases, functional + non-functional requirements, and
a list of discrete coding issues), persists them to the workspace SQLite database,
then dispatches a CodingOrchestrator for each issue.
"""

import json
from typing import Optional, Callable, List

from bizniz.base_ai_agent import BaseAIAgent
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.types import OrchestratorResult

from bizniz.engineer.types import (
    EngineeringAnalysis,
    EngineeringRequirement,
    EngineeringUseCase,
    EngineeringIssue,
    AutoEngineerBadAIResponseError,
)
from bizniz.engineer.prompts.system_prompt import AUTO_ENGINEER_SYSTEM_PROMPT
from bizniz.engineer.prompts.analyze_prompt import ANALYZE_PROMPT_TEMPLATE
from bizniz.engineer.prompts.schema import AutoEngineerSchema


class AutoEngineer(BaseAIAgent):
    """
    Software engineering analyst agent.

    analyze(problem_statement) → EngineeringAnalysis
        AI decomposes the problem into requirements, use cases, and issues.
        All artifacts are persisted to WorkspaceDB.

    dispatch(issue_id) → OrchestratorResult
        Loads an issue from the DB and runs the CodingOrchestrator on it.

    run(problem_statement) → list[OrchestratorResult]
        Full pipeline: analyze + dispatch all issues.

    Parameters
    ----------
    orchestrator_factory:
        Zero-argument callable returning a fresh CodingOrchestrator.
        A factory is used so each issue gets its own instance with a
        clean message history.
    """

    def __init__(
        self,
        client: BaseAIClient,
        environment: BaseExecutionEnvironment,
        workspace: BaseWorkspace,
        orchestrator_factory: Callable[[], CodingOrchestrator],
        max_retries: Optional[int] = 3,
        on_event: Optional[Callable] = None,
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
        self._orchestrator_factory = orchestrator_factory

    # ── BaseAIAgent contract ────────────────────────────────────────────────────

    @property
    def _process_system_prompt(self) -> str:
        return AUTO_ENGINEER_SYSTEM_PROMPT

    # ── Public API ─────────────────────────────────────────────────────────────

    def analyze(self, problem_statement: str) -> EngineeringAnalysis:
        """
        Call the AI to decompose problem_statement, persist all artifacts to the
        WorkspaceDB, and return a populated EngineeringAnalysis.
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        log("AutoEngineer: saving problem statement...")
        problem_id = self._workspace.db.save_problem(problem_statement)

        log("AutoEngineer: calling AI for engineering analysis...")
        user_prompt = ANALYZE_PROMPT_TEMPLATE.format(
            problem_statement=problem_statement
        )
        raw = self._call_ai_for_analysis(user_prompt)

        log("AutoEngineer: persisting analysis to workspace DB...")
        analysis = self._persist_analysis(problem_id, raw)
        log(
            f"AutoEngineer: analysis complete — "
            f"{len(analysis.requirements)} requirements, "
            f"{len(analysis.use_cases)} use cases, "
            f"{len(analysis.issues)} issues."
        )
        return analysis

    def dispatch(self, issue_id: int) -> OrchestratorResult:
        """
        Load an issue from the DB and run the CodingOrchestrator for it.
        Updates issue status: open → in_progress on start, → closed on success.
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        row = self._workspace.db.get_issue(issue_id)
        if row is None:
            raise ValueError(f"Issue {issue_id} not found in workspace DB.")

        self._workspace.db.update_issue_status(issue_id, "in_progress")
        log(f"AutoEngineer: dispatching orchestrator for issue #{issue_id} — {row['title']}")

        orchestrator = self._orchestrator_factory()
        result = orchestrator.run(
            prompt=row["description"],
            code_filename=row["code_file"],
            test_filename=row["test_file"],
        )

        if result.success:
            self._workspace.db.close_issue(issue_id)
            log(f"AutoEngineer: issue #{issue_id} closed successfully.")
        else:
            self._workspace.db.update_issue_status(issue_id, "open")
            log(f"AutoEngineer: issue #{issue_id} could not be resolved — reset to open.")

        return result

    def run(self, problem_statement: str) -> List[OrchestratorResult]:
        """
        Full pipeline: analyze the problem statement, then dispatch the
        CodingOrchestrator for every generated issue.
        """
        analysis = self.analyze(problem_statement)
        results = []
        for issue in analysis.issues:
            result = self.dispatch(issue.db_id)
            results.append(result)
        return results

    def close(self):
        """Close the underlying database connection (if open)."""
        if self._workspace._db is not None:
            self._workspace._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Private helpers ────────────────────────────────────────────────────────

    def _call_ai_for_analysis(self, user_prompt: str) -> dict:
        """
        Send the analysis prompt to the AI and return the parsed JSON dict.
        Retries up to max_retries on bad or empty responses.
        """
        attempts = self.max_retries
        last_error = None
        text = None

        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=AutoEngineerSchema,
                )
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    continue

                text = self.clean_llm_json(text)
                return json.loads(text)

            except Exception as e:
                last_error = e
                continue

        raise AutoEngineerBadAIResponseError(
            f"AI failed to produce engineering analysis after {attempts} attempts. "
            f"Last error: {last_error}"
        )

    def _persist_analysis(self, problem_id: int, raw: dict) -> EngineeringAnalysis:
        """
        Walk the raw AI response, write rows to WorkspaceDB, and return an
        EngineeringAnalysis with all db_id fields populated.
        """
        requirements: List[EngineeringRequirement] = []
        use_cases: List[EngineeringUseCase] = []
        issues: List[EngineeringIssue] = []

        # Business requirements
        for text in raw.get("business_requirements", []):
            db_id = self._workspace.db.save_requirement(problem_id, "business", text)
            requirements.append(EngineeringRequirement(db_id=db_id, type="business", text=text))

        # Use cases
        for uc in raw.get("use_cases", []):
            db_id = self._workspace.db.save_use_case(problem_id, uc["title"], uc["description"])
            use_cases.append(EngineeringUseCase(db_id=db_id, title=uc["title"], description=uc["description"]))

        # Functional requirements
        for text in raw.get("functional_requirements", []):
            db_id = self._workspace.db.save_requirement(problem_id, "functional", text)
            requirements.append(EngineeringRequirement(db_id=db_id, type="functional", text=text))

        # Non-functional requirements
        for text in raw.get("nonfunctional_requirements", []):
            db_id = self._workspace.db.save_requirement(problem_id, "nonfunctional", text)
            requirements.append(EngineeringRequirement(db_id=db_id, type="nonfunctional", text=text))

        # Issues — deduplicate filenames before saving
        seen_code_files: set = set()
        seen_test_files: set = set()

        for idx, issue in enumerate(raw.get("issues", []), start=1):
            code_file = _ensure_unique(issue["code_file"], seen_code_files, idx)
            test_file = _ensure_unique(issue["test_file"], seen_test_files, idx)
            seen_code_files.add(code_file)
            seen_test_files.add(test_file)

            db_id = self._workspace.db.save_issue(
                problem_id=problem_id,
                title=issue["title"],
                description=issue["description"],
                code_file=code_file,
                test_file=test_file,
            )
            issues.append(EngineeringIssue(
                db_id=db_id,
                title=issue["title"],
                description=issue["description"],
                code_file=code_file,
                test_file=test_file,
            ))

        return EngineeringAnalysis(
            problem_id=problem_id,
            requirements=requirements,
            use_cases=use_cases,
            issues=issues,
        )


# ── Utilities ──────────────────────────────────────────────────────────────────

def _ensure_unique(filename: str, seen: set, idx: int) -> str:
    """Append _<idx> suffix before the extension if the filename is already taken."""
    if filename not in seen:
        return filename
    stem, _, ext = filename.rpartition(".")
    return f"{stem}_{idx}.{ext}" if ext else f"{filename}_{idx}"

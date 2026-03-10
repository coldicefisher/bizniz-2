"""
AutoEngineer

Takes a problem statement, uses AI to produce structured engineering artifacts
(business requirements, use cases, functional + non-functional requirements, and
a list of discrete coding issues), persists them to the workspace SQLite database,
then dispatches a CodingOrchestrator for each issue.
"""

import json
import time
from typing import Optional, Callable, List

from bizniz.base_ai_agent import BaseAIAgent
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.strategy import CodingStrategy
from bizniz.clients.errors import AIInsufficientFunds
from bizniz.orchestrator.types import OrchestratorResult, OrchestratorMaxIterationsError

from bizniz.engineer.types import (
    EngineeringAnalysis,
    EngineeringRequirement,
    EngineeringUseCase,
    EngineeringIssue,
    TargetFile,
    ArchitecturePlan,
    ArchitectureNamespace,
    DomainModelDefinition,
    DomainModelField,
    MethodSignature,
    ModuleDefinition,
    DependencyEdge,
    DriftItem,
    DriftReport,
    GovernanceDecision,
    AutoEngineerBadAIResponseError,
)
from bizniz.engineer.prompts.system_prompt import AUTO_ENGINEER_SYSTEM_PROMPT, get_engineer_system_prompt
from bizniz.engineer.prompts.analyze_prompt import ANALYZE_PROMPT_TEMPLATE, get_analyze_prompt
from bizniz.engineer.prompts.plan_prompt import ARCHITECTURE_PLAN_PROMPT_TEMPLATE, get_architecture_plan_prompt
from bizniz.engineer.prompts.governance_prompt import GOVERNANCE_PROMPT_TEMPLATE
from bizniz.engineer.prompts.schema import (
    AutoEngineerSchema,
    ArchitecturePlanSchema,
    ArchitectureGovernanceSchema,
)


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
        orchestrator_factory: Callable[..., CodingOrchestrator],
        max_retries: Optional[int] = 3,
        on_event: Optional[Callable] = None,
        on_status_message: Optional[Callable[[str], None]] = None,
        language: str = "python",
    ):
        self._language = language  # Must be set before super().__init__ reads _process_system_prompt
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
        return get_engineer_system_prompt(self._language)

    # ── Public API ─────────────────────────────────────────────────────────────

    def analyze(self, problem_statement: str) -> EngineeringAnalysis:
        """
        Call the AI to decompose problem_statement, plan the architecture,
        persist all artifacts to the WorkspaceDB, and return a populated
        EngineeringAnalysis with an ArchitecturePlan.
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        log("AutoEngineer: saving problem statement...")
        problem_id = self._workspace.db.save_problem(problem_statement)

        # Step 1: Initial analysis (requirements, use cases, issues)
        log("AutoEngineer: calling AI for engineering analysis...")
        user_prompt = get_analyze_prompt(self._language).format(
            problem_statement=problem_statement,
            architecture_context="",
        )
        raw = self._call_ai_for_analysis(user_prompt)

        log("AutoEngineer: persisting analysis to workspace DB...")
        analysis = self._persist_analysis(problem_id, raw)

        # Step 2: Architecture planning
        log("AutoEngineer: planning architecture...")
        plan = self.plan_architecture(problem_id, analysis)
        analysis.architecture = plan

        # Step 3: Re-analyze with architecture context so issues reference the plan
        arch_context = self.format_architecture_context(plan)
        log("AutoEngineer: refining issues with architecture context...")
        self.clear_message_history()
        refined_prompt = get_analyze_prompt(self._language).format(
            problem_statement=problem_statement,
            architecture_context=f"ARCHITECTURE PLAN:\n{arch_context}",
        )
        refined_raw = self._call_ai_for_analysis(refined_prompt)

        # Replace issues with architecture-aware ones (keep requirements/use cases)
        analysis.issues = []
        for issue in refined_raw.get("issues", []):
            target_files = issue.get("target_files", [])
            test_files = issue.get("test_files", [])
            suggested_model = issue.get("suggested_model")
            db_id = self._workspace.db.save_issue(
                problem_id=problem_id,
                title=issue["title"],
                description=issue["description"],
                target_files=target_files,
                test_files=test_files,
                suggested_model=suggested_model,
            )
            analysis.issues.append(EngineeringIssue(
                db_id=db_id,
                title=issue["title"],
                description=issue["description"],
                target_files=[TargetFile(**tf) for tf in target_files],
                test_files=test_files,
                suggested_model=suggested_model,
            ))

        # Step 4: Create the workspace package structure
        if self._language == "typescript":
            log("AutoEngineer: skipping Python package structure for TypeScript project")
        else:
            log("AutoEngineer: creating package structure...")
            self.create_package_structure(plan)

        log(
            f"AutoEngineer: analysis complete — "
            f"{len(analysis.requirements)} requirements, "
            f"{len(analysis.use_cases)} use cases, "
            f"{len(analysis.issues)} issues, "
            f"architecture: {plan.package_name} "
            f"({len(plan.namespaces)} namespaces, "
            f"{len(plan.domain_models)} domain models, "
            f"{len(plan.modules)} modules)."
        )
        return analysis

    def dispatch(
        self,
        issue_id: int,
        workspace_context: Optional[dict] = None,
    ) -> OrchestratorResult:
        """
        Load an issue from the DB and run the CodingOrchestrator for it.

        Retry strategies on failure:
        1. TDD (default) — tests first, fix code only
        2. Strategy flip — if TDD fails, retry with CODE_FIRST
        3. Re-prompt — rewrite issue description using failure context
        4. Decompose — break into sub-issues and dispatch each
        5. Scope reduction — simplify and retry

        Parameters
        ----------
        workspace_context:
            Optional dict of existing workspace files {filepath: content}
            from previously resolved issues (cross-issue learning).
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        row = self._workspace.db.get_issue(issue_id)
        if row is None:
            raise ValueError(f"Issue {issue_id} not found in workspace DB.")

        self._workspace.db.update_issue_status(issue_id, "in_progress")
        log(f"AutoEngineer: dispatching orchestrator for issue #{issue_id} — {row['title']}")

        target_files = json.loads(row["target_files_json"]) if row["target_files_json"] else []
        test_files = json.loads(row["test_files_json"]) if row["test_files_json"] else []
        suggested_model = row["suggested_model"] if "suggested_model" in row.keys() else None

        # Load architecture context if available
        arch_context = ""
        plan_row = self._workspace.db.get_architecture_plan(row["problem_id"])
        if plan_row:
            try:
                plan_data = json.loads(plan_row["plan_json"])
                plan = ArchitecturePlan(problem_id=row["problem_id"], **plan_data)
                arch_context = self.format_architecture_context(plan)
            except Exception:
                pass

        # ── Attempt 1: TDD (default) ─────────────────────────────────────────
        result = self._run_orchestrator(
            row=row,
            target_files=target_files,
            test_files=test_files,
            arch_context=arch_context,
            suggested_model=suggested_model,
            strategy=CodingStrategy.TDD,
            workspace_context=workspace_context,
            log=log,
        )

        if result.success:
            return self._finalize_dispatch(issue_id, result, log)

        # ── Attempt 2: Flip to CODE_FIRST ────────────────────────────────────
        log(f"AutoEngineer: issue #{issue_id} failed with TDD — retrying with CODE_FIRST...")
        result = self._run_orchestrator(
            row=row,
            target_files=target_files,
            test_files=test_files,
            arch_context=arch_context,
            suggested_model=suggested_model,
            strategy=CodingStrategy.CODE_FIRST,
            workspace_context=workspace_context,
            log=log,
        )

        if result.success:
            return self._finalize_dispatch(issue_id, result, log)

        # ── Attempt 3: Re-prompt with failure context ────────────────────────
        log(f"AutoEngineer: issue #{issue_id} failed with CODE_FIRST — re-prompting...")
        reprompted = self._reprompt_issue(row, result, log)
        if reprompted:
            result = self._run_orchestrator(
                row=row,
                target_files=target_files,
                test_files=test_files,
                arch_context=arch_context,
                suggested_model=suggested_model,
                strategy=CodingStrategy.TDD,
                workspace_context=workspace_context,
                log=log,
                prompt_override=reprompted,
            )
            if result.success:
                return self._finalize_dispatch(issue_id, result, log)

        # ── Attempt 4: Scope reduction ───────────────────────────────────────
        log(f"AutoEngineer: issue #{issue_id} — trying scope reduction...")
        reduced = self._reduce_scope(row, result, log)
        if reduced:
            result = self._run_orchestrator(
                row=row,
                target_files=target_files,
                test_files=test_files,
                arch_context=arch_context,
                suggested_model=suggested_model,
                strategy=CodingStrategy.TDD,
                workspace_context=workspace_context,
                log=log,
                prompt_override=reduced,
            )
            if result.success:
                return self._finalize_dispatch(issue_id, result, log)

        # All attempts failed
        return self._finalize_dispatch(issue_id, result, log)

    def _run_orchestrator(
        self, row, target_files, test_files, arch_context,
        suggested_model, strategy, workspace_context, log,
        prompt_override=None,
    ) -> OrchestratorResult:
        """Run the orchestrator with the given strategy and return the result."""
        orchestrator = self._orchestrator_factory(suggested_model=suggested_model)
        prompt = prompt_override or row["description"]

        try:
            return orchestrator.run_multi(
                prompt=prompt,
                target_files=target_files,
                test_files=test_files,
                architecture_context=arch_context,
                initial_model=suggested_model,
                strategy=strategy,
                workspace_context=workspace_context,
            )
        except OrchestratorMaxIterationsError:
            log(f"AutoEngineer: hit max iterations with {strategy.value}")
            return OrchestratorResult(
                success=False,
                changes=[],
                test_files=[],
                iterations=orchestrator._max_iterations,
                strategy_used=strategy.value,
            )
        except AIInsufficientFunds:
            log("AutoEngineer: API account has insufficient funds — stopping.")
            raise
        except Exception as e:
            log(f"AutoEngineer: crashed ({type(e).__name__}: {e})")
            return OrchestratorResult(
                success=False,
                changes=[],
                test_files=[],
                iterations=0,
                strategy_used=strategy.value,
                failure_context=str(e),
            )

    def _finalize_dispatch(self, issue_id, result, log):
        """Handle drift governance and update issue status."""
        # Handle drift detection via governance loop
        if result.architecture_drift_detected and result.drift_files:
            row = self._workspace.db.get_issue(issue_id)
            plan_row = self._workspace.db.get_architecture_plan(row["problem_id"]) if row else None
            if plan_row:
                try:
                    plan_data = json.loads(plan_row["plan_json"])
                    plan = ArchitecturePlan(
                        db_id=plan_row["id"],
                        problem_id=row["problem_id"],
                        **plan_data,
                    )
                    drift_items = [
                        DriftItem(
                            filepath=fp,
                            drift_type="unplanned_file",
                            reason="File created by autocoder but not in architecture plan",
                        )
                        for fp in result.drift_files
                    ]
                    decision = self.review_drift(plan, drift_items)
                    if decision.decision == "approve":
                        log(f"AutoEngineer: drift approved — {decision.reason}")
                    elif decision.decision == "modify":
                        log(f"AutoEngineer: plan modified — {decision.reason}")
                    elif decision.decision == "reject":
                        log(f"AutoEngineer: drift rejected — {decision.reason}")
                except Exception as e:
                    log(f"AutoEngineer: governance review failed — {e}")

        if result.success:
            self._workspace.db.close_issue(issue_id)
            log(f"AutoEngineer: issue #{issue_id} closed successfully.")
        else:
            self._workspace.db.update_issue_status(issue_id, "open")
            log(f"AutoEngineer: issue #{issue_id} could not be resolved — reset to open.")
        return result

    def _reprompt_issue(self, row, result, log) -> Optional[str]:
        """Rewrite issue description using failure context."""
        from bizniz.engineer.prompts.retry_prompts import REPROMPT_TEMPLATE
        failure_context = result.failure_context or "No failure details available."
        strategy_used = result.strategy_used or "unknown"

        try:
            prompt = REPROMPT_TEMPLATE.format(
                title=row["title"],
                description=row["description"],
                failure_context=failure_context,
                strategy_used=strategy_used,
            )
            text, _, _ = self._client.get_text(
                messages=[Message(role="user", content=prompt)],
                use_message_history=False,
            )
            if text and text.strip():
                log(f"AutoEngineer: re-prompted issue — new description ({len(text)} chars)")
                return text.strip()
        except AIInsufficientFunds:
            raise
        except Exception as e:
            log(f"AutoEngineer: re-prompting failed — {e}")
        return None

    def _reduce_scope(self, row, result, log) -> Optional[str]:
        """Simplify issue to minimal scope using failure context."""
        from bizniz.engineer.prompts.retry_prompts import SCOPE_REDUCTION_TEMPLATE

        workspace_files = "\n".join(
            f"- {f}" for f in self._workspace.list_relative_files()
        ) or "(empty)"

        failure_context = result.failure_context or "No failure details available."

        try:
            prompt = SCOPE_REDUCTION_TEMPLATE.format(
                title=row["title"],
                description=row["description"],
                failure_context=failure_context,
                workspace_files=workspace_files,
            )
            text, _, _ = self._client.get_text(
                messages=[Message(role="user", content=prompt)],
                use_message_history=False,
            )
            if text and text.strip():
                log(f"AutoEngineer: reduced scope — new description ({len(text)} chars)")
                return text.strip()
        except AIInsufficientFunds:
            raise
        except Exception as e:
            log(f"AutoEngineer: scope reduction failed — {e}")
        return None

    def run(self, problem_statement: str) -> List[OrchestratorResult]:
        """
        Full pipeline: analyze the problem statement, then dispatch the
        CodingOrchestrator for every generated issue.

        Uses cross-issue learning: working code from resolved issues
        is passed as context to subsequent issues.
        """
        analysis = self.analyze(problem_statement)
        results = []
        workspace_context = {}

        for issue in analysis.issues:
            result = self.dispatch(issue.db_id, workspace_context=workspace_context)
            results.append(result)

            # Cross-issue learning: accumulate working code from resolved issues
            if result.success and result.changes:
                for change in result.changes:
                    workspace_context[change.filepath] = change.code

        return results

    def close(self):
        """Close the underlying database connection (if open)."""
        if self._workspace._db is not None:
            self._workspace._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Architecture Planning ────────────────────────────────────────────────

    def plan_architecture(
        self,
        problem_id: int,
        analysis: EngineeringAnalysis,
    ) -> ArchitecturePlan:
        """
        Call the AI to produce an ArchitecturePlan based on the analysis,
        persist it to the workspace DB, and return it.
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Format requirements and use cases for the prompt
        req_parts = []
        for req in analysis.requirements:
            req_parts.append(f"[{req.type}] {req.text}")
        requirements_text = "\n".join(req_parts) or "(none)"

        uc_parts = []
        for uc in analysis.use_cases:
            uc_parts.append(f"- {uc.title}: {uc.description}")
        use_cases_text = "\n".join(uc_parts) or "(none)"

        # Get the problem statement
        problem_row = self._workspace.db.get_problem(problem_id)
        problem_statement = problem_row["statement"] if problem_row else "(unknown)"

        user_prompt = get_architecture_plan_prompt(self._language).format(
            problem_statement=problem_statement,
            requirements_text=requirements_text,
            use_cases_text=use_cases_text,
        )

        log("AutoEngineer: calling AI for architecture plan...")
        raw = self._call_ai_for_plan(user_prompt)

        log("AutoEngineer: persisting architecture plan...")
        plan = self._persist_architecture_plan(problem_id, raw)
        log(
            f"AutoEngineer: architecture plan complete — "
            f"package={plan.package_name}, "
            f"{len(plan.namespaces)} namespaces, "
            f"{len(plan.domain_models)} domain models, "
            f"{len(plan.modules)} modules."
        )
        return plan

    def create_package_structure(self, plan: ArchitecturePlan):
        """
        Create the workspace directory structure from the architecture plan:
        pyproject.toml and root package directory with __init__.py.

        Only the root package directory is created here. Subdirectory namespaces
        (e.g. models/, api/) are NOT pre-created because the autocoder may generate
        single files (models.py) instead of packages (models/__init__.py), and
        having both causes Python import collisions.
        """
        self._workspace.init_as_package(
            package_name=plan.package_name,
            description=f"Generated package: {plan.package_name}",
        )

    def review_drift(
        self,
        plan: ArchitecturePlan,
        drift_items: List[DriftItem],
    ) -> GovernanceDecision:
        """
        Call the AI to review unplanned changes (drift) against the architecture plan.
        Returns a GovernanceDecision: approve, reject, or modify.
        """

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        # Use compact architecture summary instead of full plan JSON
        architecture_summary = self.format_architecture_context(plan)

        drift_parts = []
        for item in drift_items:
            drift_parts.append(
                f"- {item.filepath} ({item.drift_type}): {item.reason}"
                + (f" [class: {item.class_name}]" if item.class_name else "")
            )
        drift_description = "\n".join(drift_parts)

        user_prompt = GOVERNANCE_PROMPT_TEMPLATE.format(
            architecture_summary=architecture_summary,
            drift_description=drift_description,
        )

        log("AutoEngineer: reviewing architecture drift...")
        raw = self._call_ai_for_governance(user_prompt)

        # plan_updates comes as a JSON string from the AI — parse it
        plan_updates_raw = raw.get("plan_updates", "")
        plan_updates = None
        if plan_updates_raw and isinstance(plan_updates_raw, str) and plan_updates_raw.strip():
            try:
                plan_updates = json.loads(plan_updates_raw)
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(plan_updates_raw, dict):
            plan_updates = plan_updates_raw

        decision = GovernanceDecision(
            decision=raw["decision"],
            reason=raw["reason"],
            plan_updates=plan_updates,
        )

        log(f"AutoEngineer: governance decision — {decision.decision}: {decision.reason}")

        # If decision is "modify", update the plan in DB
        if decision.decision == "modify" and decision.plan_updates and plan.db_id:
            try:
                updates = decision.plan_updates
                current_data = json.loads(plan.json())
                for key in ["namespaces", "domain_models", "modules", "dependencies"]:
                    if key in updates:
                        current_data.setdefault(key, []).extend(updates[key])
                self._workspace.db.update_architecture_plan(
                    plan.db_id,
                    json.dumps(current_data),
                )
                log("AutoEngineer: architecture plan updated.")
            except Exception as e:
                log(f"AutoEngineer: failed to apply plan updates — {e}")

        return decision

    @staticmethod
    def format_architecture_context(plan: ArchitecturePlan) -> str:
        """
        Format an ArchitecturePlan as a human-readable string suitable for
        inclusion in prompts as architecture context.
        """
        parts = [
            f"Package: {plan.package_name}",
            f"Root namespace: {plan.root_namespace}",
        ]

        if plan.namespaces:
            parts.append("\nNamespaces:")
            for ns in plan.namespaces:
                parts.append(f"  - {ns.namespace_path}: {ns.purpose}")

        if plan.domain_models:
            parts.append("\nDomain Models:")
            for dm in plan.domain_models:
                parts.append(f"  - {dm.class_name} ({dm.filepath})")
                if dm.fields:
                    for f in dm.fields:
                        parts.append(f"      {f.name}: {f.type_hint} — {f.description}")
                if dm.methods:
                    for m in dm.methods:
                        parts.append(f"      {m.signature} — {m.description}")

        if plan.modules:
            parts.append("\nModules:")
            for mod in plan.modules:
                name = mod.class_name or "(module-level)"
                parts.append(f"  - {name} ({mod.filepath})")
                if mod.methods:
                    for m in mod.methods:
                        parts.append(f"      {m.signature} — {m.description}")

        if plan.dependencies:
            parts.append("\nDependencies:")
            for dep in plan.dependencies:
                symbols = ", ".join(dep.import_symbols) if dep.import_symbols else "*"
                parts.append(f"  - {dep.source_filepath} → {dep.target_filepath} [{symbols}]")

        return "\n".join(parts)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _call_ai_for_plan(self, user_prompt: str) -> dict:
        """Call AI for architecture plan and return parsed JSON."""
        attempts = self.max_retries
        last_error = None
        text = None

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        self.clear_message_history()
        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                log(f"AutoEngineer: AI plan call (attempt {attempt}/{attempts})...")
                t0 = time.time()
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=ArchitecturePlanSchema,
                )
                elapsed = time.time() - t0
                log(f"AutoEngineer: AI plan responded in {elapsed:.1f}s ({len(text or '')} chars)")
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    log(f"AutoEngineer: empty plan response on attempt {attempt}")
                    continue

                text = self.clean_llm_json(text)
                return json.loads(text)

            except AIInsufficientFunds:
                raise
            except Exception as e:
                last_error = e
                log(f"AutoEngineer: plan attempt {attempt} failed — {type(e).__name__}: {e}")
                continue

        raise AutoEngineerBadAIResponseError(
            f"AI failed to produce architecture plan after {attempts} attempts. "
            f"Last error: {last_error}"
        )

    def _call_ai_for_governance(self, user_prompt: str) -> dict:
        """Call AI for governance decision and return parsed JSON."""
        attempts = self.max_retries
        last_error = None
        text = None

        self.clear_message_history()
        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        for attempt in range(1, attempts + 1):
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=ArchitectureGovernanceSchema,
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
            f"AI failed to produce governance decision after {attempts} attempts. "
            f"Last error: {last_error}"
        )

    def _persist_architecture_plan(self, problem_id: int, raw: dict) -> ArchitecturePlan:
        """
        Walk the raw AI response, write rows to WorkspaceDB, and return an
        ArchitecturePlan with db_id fields populated.
        """
        # Save main plan record
        plan_id = self._workspace.db.save_architecture_plan(
            problem_id=problem_id,
            package_name=raw["package_name"],
            root_namespace=raw["root_namespace"],
            plan_json=json.dumps(raw),
        )

        # Parse and save namespaces
        namespaces = []
        namespace_id_map = {}  # namespace_path → db_id
        for ns_raw in raw.get("namespaces", []):
            ns_id = self._workspace.db.save_namespace(
                plan_id, ns_raw["namespace_path"], ns_raw["purpose"]
            )
            namespace_id_map[ns_raw["namespace_path"]] = ns_id
            namespaces.append(ArchitectureNamespace(
                db_id=ns_id,
                namespace_path=ns_raw["namespace_path"],
                purpose=ns_raw["purpose"],
            ))

        # Parse and save domain models
        domain_models = []
        for dm_raw in raw.get("domain_models", []):
            ns_id = namespace_id_map.get(dm_raw.get("namespace_path", ""))
            dm_id = self._workspace.db.save_domain_model(
                plan_id=plan_id,
                class_name=dm_raw["class_name"],
                filepath=dm_raw["filepath"],
                definition_json=json.dumps(dm_raw),
                namespace_id=ns_id,
            )
            domain_models.append(DomainModelDefinition(
                db_id=dm_id,
                class_name=dm_raw["class_name"],
                filepath=dm_raw["filepath"],
                namespace_path=dm_raw.get("namespace_path", ""),
                fields=[DomainModelField(**f) for f in dm_raw.get("fields", [])],
                methods=[MethodSignature(**m) for m in dm_raw.get("methods", [])],
                docstring=dm_raw.get("docstring", ""),
            ))

        # Parse and save modules
        modules = []
        for mod_raw in raw.get("modules", []):
            ns_id = namespace_id_map.get(mod_raw.get("namespace_path", ""))
            mod_id = self._workspace.db.save_architecture_module(
                plan_id=plan_id,
                filepath=mod_raw["filepath"],
                definition_json=json.dumps(mod_raw),
                class_name=mod_raw.get("class_name") or None,
                namespace_id=ns_id,
            )
            modules.append(ModuleDefinition(
                db_id=mod_id,
                filepath=mod_raw["filepath"],
                class_name=mod_raw.get("class_name") or None,
                namespace_path=mod_raw.get("namespace_path", ""),
                methods=[MethodSignature(**m) for m in mod_raw.get("methods", [])],
                docstring=mod_raw.get("docstring", ""),
            ))

        # Parse and save dependencies
        dependencies = []
        for dep_raw in raw.get("dependencies", []):
            self._workspace.db.save_dependency(
                plan_id=plan_id,
                source_filepath=dep_raw["source_filepath"],
                target_filepath=dep_raw["target_filepath"],
                import_symbols=json.dumps(dep_raw.get("import_symbols", [])),
            )
            dependencies.append(DependencyEdge(
                source_filepath=dep_raw["source_filepath"],
                target_filepath=dep_raw["target_filepath"],
                import_symbols=dep_raw.get("import_symbols", []),
            ))

        return ArchitecturePlan(
            db_id=plan_id,
            problem_id=problem_id,
            package_name=raw["package_name"],
            root_namespace=raw["root_namespace"],
            namespaces=namespaces,
            domain_models=domain_models,
            modules=modules,
            dependencies=dependencies,
        )

    def _call_ai_for_analysis(self, user_prompt: str) -> dict:
        """
        Send the analysis prompt to the AI and return the parsed JSON dict.
        Retries up to max_retries on bad or empty responses.
        """
        attempts = self.max_retries
        last_error = None
        text = None

        self.add_messages_to_history([Message(role="user", content=user_prompt)])

        def log(msg: str):
            if self._on_status_message:
                self._on_status_message(msg)

        for attempt in range(1, attempts + 1):
            try:
                log(f"AutoEngineer: AI analysis call (attempt {attempt}/{attempts})...")
                t0 = time.time()
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=AutoEngineerSchema,
                )
                elapsed = time.time() - t0
                log(f"AutoEngineer: AI responded in {elapsed:.1f}s ({len(text or '')} chars)")
                self.add_messages_to_history(output_messages)

                if not text or not text.strip():
                    last_error = "Empty response from AI"
                    log(f"AutoEngineer: empty response on attempt {attempt}")
                    continue

                text = self.clean_llm_json(text)
                return json.loads(text)

            except AIInsufficientFunds:
                raise
            except Exception as e:
                last_error = e
                log(f"AutoEngineer: attempt {attempt} failed — {type(e).__name__}: {e}")
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

        # Issues
        for issue in raw.get("issues", []):
            target_files = issue.get("target_files", [])
            test_files = issue.get("test_files", [])
            depends_on = issue.get("depends_on", [])
            suggested_model = issue.get("suggested_model")

            db_id = self._workspace.db.save_issue(
                problem_id=problem_id,
                title=issue["title"],
                description=issue["description"],
                target_files=target_files,
                test_files=test_files,
                suggested_model=suggested_model,
            )
            issues.append(EngineeringIssue(
                db_id=db_id,
                title=issue["title"],
                description=issue["description"],
                target_files=[TargetFile(**tf) for tf in target_files],
                test_files=test_files,
                suggested_model=suggested_model,
            ))

        return EngineeringAnalysis(
            problem_id=problem_id,
            requirements=requirements,
            use_cases=use_cases,
            issues=issues,
        )

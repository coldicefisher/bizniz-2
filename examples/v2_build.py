"""v2-build — top-level CLI for the v2 pipeline driver.

Usage:
  v2-build "<problem statement>" --project <slug>
  v2-build "<problem statement>" --project <slug> --plan-only
  v2-build "<problem statement>" --project <slug> --milestone 2
  v2-build --project <slug> --resume
  v2-build "<problem statement>" --project <slug> --auto

Reads ``bizniz.yaml`` from the current directory (or parents) for model
config. Writes per-run state to ``~/bizniz_projects/<slug>/docs/runs/<job_id>/``.

This is a thin shell: argparse + wiring. The driver lives in
``bizniz/driver/`` and is fully testable without going through here.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Ensure repo root is on PYTHONPATH when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bizniz.architect.architect import Architect
from bizniz.auth_agent.agent import AuthAgent
from bizniz.auth_operator import FusionAuthOperator
from bizniz.auth_orchestrators.fusionauth_orchestrator import FusionAuthOrchestrator
from bizniz.auth_planner import AuthPlanner
from bizniz.code_reviewer.agent import CodeReviewer
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.cost import get_tracker
from bizniz.cost.ledger import CostLedger, get_default_ledger_path
from bizniz.coder.agent import Coder
from bizniz.driver.gates import GatePolicy
from bizniz.driver.integration_phase import IntegrationPhase
from bizniz.driver.final_tester import FinalTester
from bizniz.driver.smoke_phase import SmokePhase
from bizniz.driver.document_recovery import DocumentRecovery
from bizniz.driver.smoke_recovery import SmokeRecovery
from bizniz.driver.ux_phase import UXPhase
from bizniz.driver.refactor_phase import RefactorPhase
from bizniz.driver.milestone_code_dispatcher import MilestoneCodeDispatcher
from bizniz.driver.milestone_loop import MilestoneLoop
from bizniz.driver.pipeline import V2Pipeline
from bizniz.driver.state import RunState
from bizniz.agents.debugger.agentic import AgenticDebugger
from bizniz.engineer.agent import Engineer
from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
from bizniz.environment.python_environment import PythonSandboxExecutionEnvironment
from bizniz.lib.model_progression import ModelProgression
from bizniz.service_planner.agent import ServicePlanner
from bizniz.integration.http_api_tester import HTTPApiTester
from bizniz.integration.web_ui_tester import WebUITester
from bizniz.integration.worker_tester import WorkerTester
from bizniz.planner.planner import Planner
from bizniz.provisioner.provisioner import Provisioner
from bizniz.quality_engineer.agent import QualityEngineer
from bizniz.project.project import Project
from bizniz.sidecars import ensure_sidecars_built
from bizniz.state.issue_store import IssueStateStore
from bizniz.workspace.local_workspace import LocalWorkspace


def _client_for(model: str, agent_label: str = "unknown"):
    """Build a client for ``model`` using the prefix-routing convention.

    ``agent_label`` is stamped onto the client as ``_caller_agent`` so
    the cost tracker can group records per agent (clients auto-record
    on every call and read this attribute).

    Prefix routing:
      - ``claude-cli`` / ``claude-cli:*`` → subprocess to the Claude
        Code CLI (free on Max plan; uses user's logged-in session).
      - ``claude-*``                       → Anthropic API client (paid).
      - ``gemini-*``                       → Gemini API client.
      - otherwise                          → ChatGPT client (OpenAI).
    """
    if model.startswith("claude-cli"):
        from bizniz.clients.claude_cli import ClaudeCliClient
        client = ClaudeCliClient(model_name=model)
    elif model.startswith("claude"):
        from bizniz.clients.claude.claude_client import ClaudeClient
        client = ClaudeClient(model_name=model)
    elif model.startswith("gemini"):
        from bizniz.clients.gemini.gemini_client import GeminiClient
        client = GeminiClient(model_name=model)
    else:
        from bizniz.clients.openai.chatgpt_client import ChatGPTClient
        client = ChatGPTClient(model_name=model)
    client._caller_agent = agent_label
    return client


def _on_status(prefix: str = ""):
    """Standard stdout status callback with optional prefix."""
    def cb(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {prefix}{msg}" if prefix else f"[{ts}] {msg}"
        print(line, flush=True)
    return cb


def _resolve_fa_endpoint(project_root: Path) -> tuple[str, str]:
    """Read FUSIONAUTH_HOST_URL + FUSIONAUTH_API_KEY from the project's
    generated infra/.env. Falls back to env vars if the file isn't there
    (e.g., in tests). The provisioner emits these every run.
    """
    env_path = project_root / "infra" / "development" / ".env"
    url = ""
    key = ""
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("FUSIONAUTH_HOST_URL="):
                url = line.split("=", 1)[1].strip()
            elif line.startswith("FUSIONAUTH_API_KEY="):
                key = line.split("=", 1)[1].strip()
    return (
        url or os.environ.get("FUSIONAUTH_HOST_URL", "http://localhost:9011"),
        key or os.environ.get("FUSIONAUTH_API_KEY", ""),
    )


def _build_v3_refactorer_adapter(
    *,
    project_root: Path,
    on_status: Optional[callable] = None,
):
    """Build the v3 RefactorerAgent wrapped in a v1-compat adapter.

    v1's ``Refactorer.run(milestone, architecture, is_final_milestone)``
    is what ``RefactorPhase`` calls; v3's ``V3RefactorerAgent.run()``
    takes no args. Adapter logs the milestone context and delegates.

    The agents inside (decision gate, destination planner,
    misplacement scanner) need an LLM invoker. For now we use a
    minimal Claude CLI subprocess invoker that mirrors what
    ``AgenticPhaseRecovery`` does — single ``claude --print`` call,
    returns the result text. Backend swap point lives here.
    """
    from bizniz.refactorer.decision_gate import DecisionGate
    from bizniz.refactorer.destination_planner import DestinationPlanner
    from bizniz.refactorer.extraction_executor import ExtractionExecutor
    from bizniz.refactorer.import_verifier import ImportVerifier
    from bizniz.refactorer.misplacement_scanner import MisplacementScanner
    from bizniz.refactorer.v3_agent import V3RefactorerAgent

    invoker = _make_claude_cli_invoker(project_root=project_root)

    decision_gate = DecisionGate(
        llm_invoker=invoker,
        on_status=on_status,
    )
    destination_planner = DestinationPlanner(
        project_root=project_root,
        llm_invoker=invoker,
        on_status=on_status,
    )
    misplacement_scanner = MisplacementScanner(
        project_root=project_root,
        llm_invoker=invoker,
        on_status=on_status,
    )
    executor = ExtractionExecutor(
        project_root=project_root,
        on_status=on_status,
    )
    import_verifier = ImportVerifier(
        search_roots=[
            project_root,
            project_root / "core" / "python",
        ],
    )
    agent = V3RefactorerAgent(
        project_root=project_root,
        executor=executor,
        decision_gate=decision_gate,
        destination_planner=destination_planner,
        misplacement_scanner=misplacement_scanner,
        import_verifier=import_verifier,
        on_status=on_status,
    )
    return _V3RefactorPhaseAdapter(agent=agent, on_status=on_status)


def _make_claude_cli_invoker(project_root: Path):
    """Return a callable ``(system_prompt, user_prompt) -> str`` that
    shells out to ``claude --print`` and returns the result text.

    Used by v3 refactorer's gate + destination planner + scanner.
    Mirrors the dispatch shape in ``AgenticPhaseRecovery``; kept
    inline here because v3's collaborators take a simpler
    invoker contract (no tool calls — just text → text).
    """
    import json as _json
    import shutil as _shutil
    import subprocess as _subprocess

    DEFAULT_TOOLS = ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]

    def invoke(system_prompt: str, user_prompt: str) -> str:
        if _shutil.which("claude") is None:
            return ""
        cmd = [
            "claude", "--print",
            "--output-format=json",
            "--append-system-prompt", system_prompt,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(DEFAULT_TOOLS),
            "--add-dir", str(project_root),
        ]
        proc = _subprocess.run(
            cmd, input=user_prompt, capture_output=True, text=True,
            timeout=900, cwd=str(project_root),
        )
        if proc.returncode != 0:
            return ""
        try:
            payload = _json.loads(proc.stdout)
        except _json.JSONDecodeError:
            return ""
        if payload.get("is_error"):
            return ""
        return payload.get("result") or ""

    return invoke


class _V3RefactorPhaseAdapter:
    """Bridges ``V3RefactorerAgent.run()`` (no args) to RefactorPhase's
    expected ``run(milestone, architecture, is_final_milestone)``
    signature."""

    def __init__(self, agent, on_status=None):
        self._agent = agent
        self._on_status = on_status

    def run(
        self,
        milestone,
        architecture,
        is_final_milestone: bool,
    ):
        scope = "FINAL-MILESTONE" if is_final_milestone else "MID-PROJECT"
        if self._on_status:
            try:
                self._on_status(
                    f"V3Refactorer ({scope}): starting refactor pass for "
                    f"milestone '{milestone.name}' "
                    f"(M{milestone.sequence_index + 1})"
                )
            except Exception:
                pass
        return self._agent.run()


def render_workspace_summary(project_root: Path, max_files: int = 200) -> str:
    """Pre-render a compact workspace tree for the Engineer's initial context.

    Cost lever (E): one-time render saves the Engineer ~5-10 tool calls
    of `list_directory` / `get_workspace_tree` discovery in the typical
    M1 implement loop. Bloats the initial prefix by ~1-2k tokens but
    that prefix is cached (lever A) on every iteration.
    """
    from bizniz.workspace.local_workspace import LocalWorkspace
    ws = LocalWorkspace(root=project_root)
    try:
        files = ws.list_relative_files()
    except Exception:
        return ""
    files = sorted(f for f in files if not f.startswith("docs/runs/"))
    if len(files) > max_files:
        # Truncate but keep top-level + per-service directory listings.
        truncated = files[:max_files]
        return "\n".join(truncated) + f"\n\n... ({len(files) - max_files} more files truncated)"
    return "\n".join(files)


def _phase_label_for_log(args) -> str:
    """One-line greppable label for the cost log."""
    if args.plan_only:
        return "plan-only"
    if args.phase:
        if args.milestone is not None:
            return f"milestone={args.milestone} phase={args.phase}"
        return f"phase={args.phase}"
    if args.milestone is not None:
        return f"full milestone={args.milestone}"
    return "full"


def _seed_fa_env_from_project(project_root: Path) -> None:
    """Export FUSIONAUTH_* vars from the project's infra/.env into the
    process env so pipeline-internal helpers (``_resolve_fa_tenant_id``)
    can query the running FA without explicit args.
    """
    env_path = project_root / "infra" / "development" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        if line.startswith("FUSIONAUTH_") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip())


def _project_root(project_slug: str) -> Path:
    """Resolve ``<projects_root>/<slug>/``."""
    base = Path(os.environ.get("BIZNIZ_PROJECTS_ROOT") or
                str(Path.home() / "bizniz_projects"))
    return base / project_slug


def _resolve_runs_root(project_slug: str) -> Path:
    """Resolve where per-run state lives. As of 2026-05-16 (item 8A),
    new state goes to ``<project>/.bizniz/runs/``; existing projects
    with state still at ``<project>/docs/runs/`` keep working via the
    ``runs_paths.resolve_runs_root`` fallback."""
    from bizniz.driver.runs_paths import resolve_runs_root
    return resolve_runs_root(_project_root(project_slug))


def _runner_for_service(service) -> str:
    """Map a service's language to its test runner.

    Python → pytest (the v33 backend stack).
    TypeScript / JavaScript → vitest (the v33 frontend stack, and the
    current bizniz-skeleton-react default). vitest is invoked as
    ``npx vitest run`` so we don't depend on ``package.json scripts.test``
    being wired correctly. v33 lesson: ``npm test --silent -- --ci``
    blew up because vitest CACErrors on the jest-only ``--ci`` flag.
    Unknown → pytest as the safe default.
    """
    lang = (getattr(service, "language", "") or "").lower()
    if lang in ("typescript", "javascript", "ts", "js"):
        return "vitest"
    return "pytest"


def _new_job_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _build_pipeline(args, on_status) -> V2Pipeline:
    # Sidecar preflight: every test run depends on bizniz-test-pytest
    # (and bizniz-test-playwright for frontend). Build them up front
    # rather than lazy-on-first-call so the failure mode is "pipeline
    # halts at startup with a clear message" instead of "Coder iter 12
    # mysteriously fails to run tests."
    ensure_sidecars_built(on_status=on_status)

    config = BiznizConfig.find_and_load()

    planner_client = _client_for(config.planner_model or config.architect_model, "planner")
    architect_client = _client_for(config.architect_model, "architect")
    engineer_client = _client_for(config.engineer_model, "engineer")
    qe_client = _client_for(config.architect_model, "quality_engineer")
    cr_client = _client_for(config.architect_model, "code_reviewer")
    tester_client = _client_for(getattr(config, "tester_model", config.architect_model), "integration_tester")
    auth_client = _client_for(config.architect_model, "auth_agent")

    # Engineer escalation chain. Used by:
    #   - MilestoneLoop.implement: when the Engineer stalls (same action
    #     3+ times in last 5 iterations), bump to next tier.
    #   - MilestoneLoop.repair: each repair iteration uses the next tier.
    # Read engineer_escalation from config if present; fall back to a
    # cheap-to-expensive default.
    engineer_tiers = list(
        getattr(config, "engineer_escalation", []) or [
            config.engineer_model,
            "gemini-flash-top",
            "gemini-pro",
        ]
    )
    repair_tiers = list(getattr(config, "repair_escalation", []) or engineer_tiers)

    planner = Planner(client=planner_client, on_status=on_status)
    architect = Architect(client=architect_client, on_status=on_status)
    qe = QualityEngineer(client=qe_client, on_status=on_status)
    cr = CodeReviewer(client=cr_client, on_status=on_status)

    project_slug = args.project
    project_root = Path(os.environ.get("BIZNIZ_PROJECTS_ROOT") or
                        str(Path.home() / "bizniz_projects")) / project_slug
    compose_path = str(project_root / "infra" / "development" / "docker-compose.yml")

    primary_workspace = LocalWorkspace(root=project_root)

    # Seed FUSIONAUTH_* env vars so pipeline FA-ID resolvers can query
    # the running FA. No-op if the project hasn't been provisioned yet.
    _seed_fa_env_from_project(project_root)

    def workspace_for_service(name: str) -> LocalWorkspace:
        # Service workspaces live at <project>/<workspace_name>/
        return LocalWorkspace(root=project_root / name)

    engineer = Engineer(
        client=engineer_client,
        workspace=primary_workspace,
        compose_path=compose_path,
        target_service="backend",  # service-specific tools default; loop overrides per-service
        on_status=on_status,
    )

    def repair_engineer_factory(iteration: int) -> Engineer:
        tier_model = repair_tiers[min(iteration, len(repair_tiers) - 1)]
        return Engineer(
            client=_client_for(tier_model, f"engineer_repair_t{iteration}"),
            workspace=primary_workspace,
            compose_path=compose_path,
            target_service="backend",
            on_status=on_status,
        )

    def engineer_escalation_factory(tier: int) -> Engineer:
        """Returns a fresh Engineer at ``tier`` of the engineer_escalation
        chain. Tier 0 = default; raises IndexError when chain exhausted."""
        if tier >= len(engineer_tiers):
            raise IndexError(f"escalation tier {tier} exceeds chain length {len(engineer_tiers)}")
        tier_model = engineer_tiers[tier]
        return Engineer(
            client=_client_for(tier_model, f"engineer_t{tier}"),
            workspace=primary_workspace,
            compose_path=compose_path,
            target_service="backend",
            on_status=on_status,
        )

    # ── v2.5 dispatcher wiring ────────────────────────────────────────
    # ServicePlanner: shared client (planner-tier reasoning, single call
    # per service per milestone). Coder: per-tier client built fresh.
    # Progression: per-service fresh ModelProgression so escalation in
    # one service doesn't leak to another.
    sp_client = _client_for(
        getattr(config, "planner_model", None) or config.architect_model,
        "service_planner",
    )

    def service_planner_factory(_service):
        return ServicePlanner(client=sp_client, on_status=on_status)

    # Decomposer factory — roadmap item 4. **Opt-in only as of
    # 2026-05-18.** Two-fixture perf validation (BE-006 + BA-fix2-2;
    # see docs/perf_tests/) showed Decomposer is net-negative on
    # Claude CLI: 3-4× wall-clock penalty for the same 14/14 + AST
    # + symbol-clean output that bare fat dispatch produces in one
    # shot. The dense per-unit dispatch pattern also burns through
    # Max-plan usage windows faster and triggers 429 cascades.
    # Pass ``--decompose`` to opt in (kept for model-tier experiments
    # and future tier-2 fallback uses); ``--no-decompose`` is the
    # deprecated explicit form of the new default.
    decompose_requested = (
        getattr(args, "decompose", False)
        and not getattr(args, "no_decompose", False)
    )
    if decompose_requested:
        from bizniz.decomposer.agent import Decomposer
        decomposer_client = config.make_client(
            model=getattr(config, "decomposer_model", config.engineer_model)
        )

        def decomposer_factory(_service):
            return Decomposer(
                client=decomposer_client, on_status=on_status,
            )
        on_status(
            "Decomposer: enabled (--decompose) — per-unit dispatch active. "
            "ServicePlanner issues will be expanded into ordered units."
        )
    else:
        decomposer_factory = None
        on_status(
            "Decomposer: disabled (default since 2026-05-18 perf "
            "verdict). Coder gets full feature-sized issues. Use "
            "--decompose to opt in."
        )

    coder_tiers = list(
        getattr(config, "coder_models", []) or [config.engineer_model]
    )
    # Per-tier iteration budget. Lite is cheap so let it grind cheaply;
    # top has fewer turns because each one is more expensive but more
    # capable; pro is the last shot — strongest model, modest budget.
    # Counts come from the strategy discussion: 100 / 50 / 20.
    tier_iterations = {
        coder_tiers[0]: 100,
        **({coder_tiers[1]: 50} if len(coder_tiers) > 1 else {}),
        **({coder_tiers[2]: 20} if len(coder_tiers) > 2 else {}),
    }
    # Higher tiers (pro+ if config grows) inherit the last explicit
    # budget. Falls back to 30 for unknown models.
    default_iterations = (
        list(tier_iterations.values())[-1] if tier_iterations else 30
    )

    def coder_factory(model: str, service):
        # Route to ClaudeCliCoder when the model name says so. Same
        # constructor surface (kwargs ignored if irrelevant), same
        # CoderResult return type — the orchestrator + dispatcher
        # don't see the difference.
        if model.startswith("claude-cli"):
            from bizniz.coder.claude_cli_coder import ClaudeCliCoder
            return ClaudeCliCoder(
                workspace=workspace_for_service(service.workspace_name),
                compose_path=compose_path,
                target_service=service.name,
                workspace_name=service.workspace_name,
                on_status=on_status,
                runner=_runner_for_service(service),
                model_name=model,
            )
        return Coder(
            client=_client_for(model, f"coder:{service.name}"),
            workspace=workspace_for_service(service.workspace_name),
            compose_path=compose_path,
            target_service=service.name,
            workspace_name=service.workspace_name,
            tool_iterations=tier_iterations.get(model, default_iterations),
            on_status=on_status,
            runner=_runner_for_service(service),
        )

    def progression_factory(_service):
        return ModelProgression(list(coder_tiers))

    # Issue-state DB: per-project ProjectDB, scoped per (job, milestone)
    # via factory. The dispatcher writes to it as it goes; MilestoneLoop
    # reads from it for IMPLEMENT-phase resume + EngineerResult assembly.
    project_obj = Project(root=project_root, project_name=project_slug)

    def issue_store_factory(milestone_index: int) -> IssueStateStore:
        return IssueStateStore(
            db=project_obj.db,
            job_id=job_id,
            milestone_index=milestone_index,
        )

    v2_dispatcher = MilestoneCodeDispatcher(
        service_planner_factory=service_planner_factory,
        coder_factory=coder_factory,
        progression_factory=progression_factory,
        decomposer_factory=decomposer_factory,
        # Dispatcher gets a scoped store for milestone 1 by default.
        # MilestoneLoop overrides with per-milestone stores via the
        # issue_store_factory it receives (passed below).
        issue_store=None,
        on_status=on_status,
        # --retry-service / --retry-failed flow through here so the
        # dispatcher reuses existing planned issues instead of paying
        # for ServicePlanner re-runs.
        only_service=getattr(args, "retry_service", None),
        skip_planning=bool(getattr(args, "retry_failed", False)),
    )

    # ── v3 pipeline flags (Stages A + B) ───────────────────────────
    # ``--use-v3`` is the convenience shortcut that enables both
    # stages. Stage-specific flags also accepted independently:
    #   --use-v3-implement → Stage A only (IMPLEMENT dispatcher swap)
    #   --use-v3-review    → Stage B only (parallel review unit)
    #
    # ``--use-v3-1`` is the canonical post-2026-05-19 path: V3
    # IMPLEMENT (Stage A) + parallel QE+CR review with V2 approval
    # semantics + V2 per-issue repair dispatch. Implies Stage A;
    # does NOT enable Stage B's lossy UnifiedFinding adapter path.
    use_v5_flag = getattr(args, "use_v5", False)
    use_v4_flag = use_v5_flag or getattr(args, "use_v4", False)
    use_v3_1_flag = use_v4_flag or getattr(args, "use_v3_1", False)
    use_v3_shortcut = getattr(args, "use_v3", False)
    use_v3_implement_flag = (
        use_v3_1_flag
        or use_v3_shortcut
        or getattr(args, "use_v3_implement", False)
    )
    use_v3_review_flag = use_v3_shortcut or getattr(args, "use_v3_review", False)

    # ── v4 IMPLEMENT + REPAIR dispatcher ──────────────────────────
    # When ``use_v4_flag``, BOTH IMPLEMENT and REPAIR dispatch via the
    # V4 dispatcher: ServicePlannerWithScaffold + per-issue
    # CoderTesterAgent (single agent writes code AND tests) + per-issue
    # validator (deterministic gates + fix-loop) + PIRunner
    # (DAG-aware parallel ThreadPoolExecutor). Repair uses Opus-only
    # tier list (no Haiku→Opus escalation).
    if use_v4_flag:
        from bizniz.coder_tester.agent import CoderTesterAgent
        from bizniz.driver.v4_milestone_code_dispatcher import (
            V4MilestoneCodeDispatcher,
        )
        from bizniz.service_planner.scaffolded import ServicePlannerWithScaffold

        max_parallel = int(getattr(config, "max_parallel_coders", 6))
        repair_tiers = list(getattr(config, "use_v4_repair_tiers", []))
        if not repair_tiers:
            repair_tiers = ["claude-cli:claude-opus-4-7"]

        def v4_planner_factory(_service):
            sp_client = _client_for(
                getattr(config, "service_planner_model", config.engineer_model),
                f"service_planner_v4:{_service.name}",
            )
            return ServicePlannerWithScaffold(
                client=sp_client, on_status=on_status,
            )

        def v4_coder_tester_factory(service):
            # IMPLEMENT-tier CoderTester: uses the same tier-0 model as
            # v3 (Haiku by default). Per-issue scope means the model
            # sees one issue's context, not the whole milestone.
            tier0_model = coder_tiers[0] if coder_tiers else config.engineer_model
            v4_client = _client_for(
                tier0_model, f"coder_tester_v4:{service.name}",
            )
            return CoderTesterAgent(
                client=v4_client, on_status=on_status,
            )

        def v4_repair_coder_tester_factory(service):
            # REPAIR-tier CoderTester: Opus-only by default. Repair is
            # the harder case (IMPLEMENT already missed); skip the
            # Haiku tier so we don't burn the wasted retry budget.
            repair_model = repair_tiers[0]
            v4_repair_client = _client_for(
                repair_model, f"coder_tester_v4_repair:{service.name}",
            )
            return CoderTesterAgent(
                client=v4_repair_client, on_status=on_status,
            )

        # Repair planner: production ServicePlanner (has plan_repair()
        # which takes coverage + code_review + prior_issues and emits
        # fix-issues). We instantiate per-service so each gets its own
        # client (status logging, cost attribution).
        from bizniz.service_planner.agent import ServicePlanner as V4RepairPlanner

        def v4_repair_planner_factory(service):
            rp_client = _client_for(
                getattr(config, "service_planner_model", config.engineer_model),
                f"service_planner_v4_repair:{service.name}",
            )
            return V4RepairPlanner(client=rp_client, on_status=on_status)

        code_dispatcher = V4MilestoneCodeDispatcher(
            planner_factory=v4_planner_factory,
            coder_tester_factory=v4_coder_tester_factory,
            repair_coder_tester_factory=v4_repair_coder_tester_factory,
            workspace_for_service=workspace_for_service,
            max_parallel_coders=max_parallel,
            issue_store=None,
            on_status=on_status,
            only_service=getattr(args, "retry_service", None),
            repair_planner_factory=v4_repair_planner_factory,
            # Option 1: in-container pytest collection.
            compose_path=str(compose_path) if compose_path else None,
        )
        on_status(
            f"IMPLEMENT + REPAIR phase: v4 dispatcher ENABLED (--use-v4). "
            f"Per-issue CoderTesterAgent + PerIssueValidator + PIRunner "
            f"(max_parallel={max_parallel}). Repair tier: "
            f"{repair_tiers[0]} (Opus-only, no escalation chain)."
        )

    # ── v3 IMPLEMENT dispatcher (Stage A) ──────────────────────────
    # When ``use_v3_implement_flag``, IMPLEMENT phase runs a single
    # ServicePlannerWithScaffold + CoderAgentV3 per service instead of
    # today's per-issue Coder loop. Review/repair still uses the v2
    # dispatcher (delegated via .repair()) — that's Stage B's scope.
    elif use_v3_implement_flag:
        from bizniz.coder.agent_v3 import CoderAgentV3
        from bizniz.driver.v3_milestone_code_dispatcher import (
            V3MilestoneCodeDispatcher,
        )
        from bizniz.service_planner.scaffolded import ServicePlannerWithScaffold

        def v3_planner_factory(_service):
            sp_v3_client = _client_for(
                getattr(config, "service_planner_model", config.engineer_model),
                f"service_planner_v3:{_service.name}",
            )
            return ServicePlannerWithScaffold(
                client=sp_v3_client, on_status=on_status,
            )

        def v3_coder_factory(service):
            # CoderAgentV3 takes a BaseAIClient via its config-based
            # routing (claude-cli:<model>). Use the same tier list
            # the v2 Coder used (tier 0 — Haiku by default per the
            # post-2026-05-18 config). Escalation in the v3 path is
            # handled by the agent's own retry on schema-validation
            # failure, not the per-issue stall path.
            tier0_model = coder_tiers[0] if coder_tiers else config.engineer_model
            v3_client = _client_for(
                tier0_model, f"coder_agent_v3:{service.name}",
            )
            return CoderAgentV3(client=v3_client, on_status=on_status)

        code_dispatcher = V3MilestoneCodeDispatcher(
            planner_factory=v3_planner_factory,
            coder_factory=v3_coder_factory,
            workspace_for_service=workspace_for_service,
            issue_store=None,
            on_status=on_status,
            only_service=getattr(args, "retry_service", None),
            repair_dispatcher=v2_dispatcher,
        )
        on_status(
            "IMPLEMENT phase: v3 dispatcher ENABLED (--use-v3-implement). "
            "Single ServicePlannerWithScaffold + CoderAgentV3 per service. "
            "Review/repair still uses v2 dispatcher (Stage B will replace)."
        )
    else:
        code_dispatcher = v2_dispatcher

    if use_v3_1_flag:
        on_status(
            "REVIEW/REPAIR phase: v3.1 ENABLED (--use-v3-1). "
            "Parallel QE+CR review (V3 fan-out) + native CoverageReport/"
            "CodeReviewReport (no UnifiedFinding adapter) + V2 per-issue "
            "repair dispatch."
        )

    def http_tester_factory(workspace):
        return HTTPApiTester(
            client=tester_client,
            workspace=workspace,
            environment=PythonSandboxExecutionEnvironment(),
            on_status_message=on_status,
        )

    def web_tester_factory(workspace):
        return WebUITester(
            client=tester_client,
            workspace=workspace,
            environment=PythonSandboxExecutionEnvironment(),
            on_status_message=on_status,
        )

    def worker_tester_factory(workspace):
        return WorkerTester(
            client=tester_client,
            workspace=workspace,
            environment=PythonSandboxExecutionEnvironment(),
            on_status_message=on_status,
        )

    debugger_model = getattr(config, "debugger_model", config.architect_model)

    def debugger_factory(workspace, service):
        """Build a fresh debugger bound to ``service``'s container.

        Routing: ``claude-cli`` model name → ClaudeCliDebugger
        (subprocess + native tools). Anything else → legacy
        AgenticDebugger (JSON-schema action loop). Both implement
        ``diagnose(error_output, source_files, test_files,
        architecture_context, repair_history) -> AgenticDiagnosis``.
        """
        if debugger_model.startswith("claude-cli"):
            from bizniz.agents.debugger.claude_cli_debugger import ClaudeCliDebugger
            return ClaudeCliDebugger(
                workspace=workspace,
                compose_path=compose_path,
                service_name=service.name,
                on_status_message=on_status,
                model_name=debugger_model,
            )

        debugger_client = _client_for(debugger_model, "debugger")
        image = getattr(service, "image_name", None) or "python:3.12-slim"
        env = DockerPytestEnvironment(
            workspace_root=workspace.root if hasattr(workspace, "root") else project_root,
            image=image,
        )
        return AgenticDebugger(
            client=debugger_client,
            workspace=workspace,
            environment=env,
            tool_iterations=15,
            timeout_seconds=600,
            compose_path=compose_path,
            service_name=service.name,
            on_status_message=on_status,
        )

    integration_phase = IntegrationPhase(
        http_tester_factory=http_tester_factory,
        web_tester_factory=web_tester_factory,
        worker_tester_factory=worker_tester_factory,
        debugger_factory=debugger_factory,
        debugger_max_iterations=3,
        problem_statement=args.problem or "",
        on_status=on_status,
    )

    smoke_phase = SmokePhase(on_status=on_status)

    # FinalTester — end-of-milestone e2e canary, the LAST gate before
    # DONE. Verifies the stack is shippable by hitting real HTTP
    # endpoints as a user would. Delegates probe machinery to
    # SmokePhase but is a separate state-tracked phase so resume can
    # pick up at this exact point and the post-mortem report has its
    # own line for "did the final shipping gate pass?"
    final_tester = FinalTester(
        smoke_phase=smoke_phase,
        on_status=on_status,
    )

    # Smoke recovery — one-shot Claude CLI agent that tries to fix
    # smoke 5xx failures (stale process, missing migration, etc.)
    # before the pipeline hard-halts. Only available when claude is
    # on PATH; otherwise the smoke gate halts as before.
    import shutil as _shutil_smoke
    smoke_recovery = (
        SmokeRecovery(
            compose_path=compose_path,
            project_root=Path(project_root),
            on_status=on_status,
        )
        if _shutil_smoke.which("claude") is not None
        else None
    )

    # UXPhase factory: per-frontend, builds a UX designer.
    #
    # Picks the implementation by binary availability:
    #   - ``claude`` on PATH → ProUXDesigner (three-step flow with
    #     route resolver + capture-mismatch handling + plan cache +
    #     run log; subclass of ClaudeUXDesigner). $0 on Max plan.
    #   - else → legacy UXDesigner with GeminiClient inline image
    #     vision
    #
    # The UX fix path is ClaudeCliCoder regardless; only the eval +
    # iteration mechanism differs. Choosing by binary (not config)
    # means the selector survives bizniz.yaml flavors that mix
    # Claude + Gemini.
    import shutil as _shutil
    use_claude_ux = _shutil.which("claude") is not None

    # ReviewStore for the per-route cache so M2+ can skip clean
    # routes that haven't changed since M1's UX pass. Project-wide,
    # one db per project_slug. (Mirrors the debug_ux.py harness.)
    from bizniz.ux_designer.review_store import ReviewStore as _ReviewStore
    _review_store = _ReviewStore(
        Path(project_root) / ".bizniz" / "ux_reviews.db",
    )

    def ux_designer_factory(frontend_service):
        def ux_coder_factory(workspace):
            from bizniz.coder.claude_cli_coder import ClaudeCliCoder
            return ClaudeCliCoder(
                workspace=workspace,
                compose_path=compose_path,
                target_service=frontend_service.name,
                workspace_name=frontend_service.workspace_name,
                on_status=on_status,
                runner=_runner_for_service(frontend_service),
                model_name="claude-cli",
            )

        if use_claude_ux:
            from bizniz.clients.claude_cli.claude_cli_client import ClaudeCliClient
            from bizniz.ux_designer.pro_ux_designer import ProUXDesigner
            from bizniz.ux_designer.storybook_driver import StorybookDriver
            from bizniz.ux_designer.storybook_eval import StoryEvaluator
            from bizniz.ux_designer.storybook_fix import StoryFixDispatcher
            # Text-only client for screenshot script generation.
            vision_client = ClaudeCliClient(model_name="claude-cli")
            # Roadmap item 2 (2026-05-17): wire the Storybook UX
            # gate so the per-story loop runs alongside the per-route
            # loop. Storybook is the primary primitive-grade signal;
            # per-route continues to cover page-level layout, auth
            # flows, and multi-route nav that primitives can't.
            # StoryEvaluator + StoryFixDispatcher default to the
            # Claude CLI invokers (same dispatch shape as ProUXDesigner
            # already uses). The driver tears down its server even
            # on failure, so this can never leak processes.
            storybook_driver = StorybookDriver(
                evaluator=StoryEvaluator(on_status=on_status),
                fix_dispatcher=StoryFixDispatcher(on_status=on_status),
                on_status=on_status,
            )
            return ProUXDesigner(
                vision_client=vision_client,
                coder_factory=ux_coder_factory,
                on_status=on_status,
                review_store=_review_store,
                project_slug=project_slug,
                storybook_driver=storybook_driver,
            )

        from bizniz.clients.gemini.gemini_client import GeminiClient
        from bizniz.ux_designer.ux_designer import UXDesigner
        vision_client = GeminiClient(model_name="gemini-flash")
        return UXDesigner(
            vision_client=vision_client,
            coder_factory=ux_coder_factory,
            on_status=on_status,
        )

    ux_phase = UXPhase(
        ux_factory=ux_designer_factory, on_status=on_status,
    )

    # RefactorPhase factory. v1 (single-shot Refactorer) is the
    # default; opt into v3 (deterministic + agent pipeline with
    # decision gate, destination planner, import verifier, executor)
    # via ``BIZNIZ_REFACTORER=v3``. v3 lands per
    # docs/backlog/v3_refactorer_design.md.
    def refactorer_factory():
        mode = os.environ.get("BIZNIZ_REFACTORER", "v1").lower()
        if mode == "v3":
            return _build_v3_refactorer_adapter(
                project_root=project_root,
                on_status=on_status,
            )
        from bizniz.refactorer.refactorer import Refactorer
        return Refactorer(
            project_root=project_root,
            compose_path=compose_path,
            on_status=on_status,
        )

    refactor_phase = RefactorPhase(
        refactorer_factory=refactorer_factory, on_status=on_status,
    )

    # DocumentRecovery — agent that writes missing critical docs
    # when HumanDocsGenerator falls short (D17, 2026-05-17). Only
    # wired when `claude` is on PATH; otherwise the critical-docs
    # gate is a no-op (legacy best-effort behavior preserved).
    import shutil as _shutil_doc
    document_recovery = (
        DocumentRecovery(
            project_root=Path(project_root),
            on_status=on_status,
        )
        if _shutil_doc.which("claude") is not None
        else None
    )

    gate_mode = "auto" if args.auto else ("interactive" if args.interactive else "strict")
    gates = GatePolicy(mode=gate_mode, on_status=on_status)

    runs_root = _resolve_runs_root(project_slug)
    job_id = args.resume_job_id or _new_job_id()
    run_state = RunState(runs_root / job_id)

    # Cost tracker: start a job so every recorded call carries the
    # job_id; phase + milestone tags are set per-call by V2Pipeline +
    # MilestoneLoop. The run_status's job_id is reused so the cost log
    # and run state agree.
    cost_tracker = get_tracker()
    # Attach the cumulative cross-project ledger BEFORE start_job so
    # every record from this run lands in ~/.bizniz/cost_ledger.jsonl.
    # Survives project-dir wipes — the alternative is the per-run
    # costs.md which gets nuked along with the project.
    ledger = CostLedger()
    cost_tracker.attach_ledger(ledger)
    if cost_tracker.current_job_id is None:
        cost_tracker.start_job(
            project_slug=project_slug,
            problem_statement=args.problem or "",
            metadata={"run_state_job_id": job_id, "gate_mode": gate_mode},
        )
    on_status(f"Cost ledger: {ledger.path}")

    workspace_summary = render_workspace_summary(project_root)
    on_status(
        f"Workspace summary: {len(workspace_summary.splitlines())} files "
        f"pre-rendered for Engineer initial context"
    )

    # v2.5 mode caps repair at 2 iterations (ServicePlanner repair-mode
    # for both). After 2, MilestoneLoop hard-gates on milestone_unapproved.
    # Legacy v2 mode used len(repair_tiers); kept inline for the fallback.
    repair_budget_v25 = 2

    # ``use_v3_implement_flag`` / ``use_v3_review_flag`` already
    # computed above where the dispatcher swap happens.

    # ── v5 ResolutionCheckers + ProjectGit wiring ─────────────────
    # When --use-v5 is set, build per-source ResolutionCheckers (one
    # for QE flavor, one for CR flavor) plus the ProjectGit handle
    # the loop uses for snapshot + rollback.
    v5_qe_checker = None
    v5_cr_checker = None
    project_git_for_v5 = None
    milestone_debugger_for_v5 = None
    if use_v5_flag:
        from bizniz.resolution_checker.checker import ResolutionChecker
        from bizniz.driver.project_git import ProjectGit
        from bizniz.per_milestone_debugger.debugger import PerMilestoneDebugger

        qe_check_client = _client_for(
            getattr(config, "qe_model", config.engineer_model),
            "resolution_checker:qe",
        )
        cr_check_client = _client_for(
            getattr(config, "cr_model", config.engineer_model),
            "resolution_checker:cr",
        )
        v5_qe_checker = ResolutionChecker(
            client=qe_check_client, on_status=on_status,
        )
        v5_cr_checker = ResolutionChecker(
            client=cr_check_client, on_status=on_status,
        )
        project_git_for_v5 = ProjectGit(
            project_root=project_root, on_status=on_status,
        )
        try:
            milestone_debugger_for_v5 = PerMilestoneDebugger(
                project_root=project_root,
                compose_path=str(compose_path) if compose_path else None,
                timeout_seconds=3000,
                on_status=on_status,
            )
        except Exception as e:
            on_status(
                f"PerMilestoneDebugger init failed ({type(e).__name__}: "
                f"{e}) — v5 loop will run without escalation"
            )
            milestone_debugger_for_v5 = None
        on_status(
            "REVIEW/REPAIR phase: v5 ENABLED (--use-v5). "
            "Iter 1 = full review frozen as CanonicalReport; "
            "iter 2+ = ResolutionChecker (no fresh review). "
            "Regressions trigger ProjectGit rollback. "
            f"Milestone debugger: "
            f"{'wired' if milestone_debugger_for_v5 else 'unavailable'}."
        )

    milestone_loop = MilestoneLoop(
        engineer=engineer,
        quality_engineer=qe,
        code_reviewer=cr,
        integration_phase=integration_phase,
        smoke_phase=smoke_phase,
        smoke_recovery=smoke_recovery,
        gates=gates,
        workspace_for_service=workspace_for_service,
        primary_workspace=primary_workspace,
        compose_path=compose_path,
        project_root=project_root,
        repair_budget=repair_budget_v25,
        repair_engineer_factory=repair_engineer_factory,
        engineer_escalation_factory=engineer_escalation_factory,
        code_dispatcher=code_dispatcher,
        use_v3_review_unit=use_v3_review_flag,
        use_v3_1=use_v3_1_flag,
        use_v5=use_v5_flag,
        v5_qe_checker=v5_qe_checker if use_v5_flag else None,
        v5_cr_checker=v5_cr_checker if use_v5_flag else None,
        project_git=project_git_for_v5 if use_v5_flag else None,
        milestone_debugger=milestone_debugger_for_v5 if use_v5_flag else None,
        issue_store_factory=issue_store_factory,
        cost_tracker=cost_tracker,
        workspace_summary=workspace_summary,
        ux_phase=ux_phase,
        refactor_phase=refactor_phase,
        final_tester=final_tester,
        smoke_recovery_stall_threshold=getattr(
            config, "debugger_stall_threshold", 5,
        ),
        # repair_stall_threshold now has its own config key (default
        # 3, down from 5 — see BiznizConfig.repair_stall_threshold).
        # Fall back to debugger_stall_threshold for back-compat.
        repair_stall_threshold=getattr(
            config, "repair_stall_threshold",
            getattr(config, "debugger_stall_threshold", 3),
        ),
        document_recovery=document_recovery,
        document_recovery_stall_threshold=getattr(
            config, "debugger_stall_threshold", 5,
        ),
        on_status=on_status,
    )

    def auth_agent_factory(architecture):
        fa_url, fa_key = _resolve_fa_endpoint(project_root)
        fa_orch = FusionAuthOrchestrator(
            base_url=fa_url,
            api_key=fa_key,
            on_status=on_status,
        )
        return AuthAgent(
            client=auth_client,
            workspace=primary_workspace,
            fa_orchestrator=fa_orch,
            on_status=on_status,
        )

    # v2.6 split-AuthAgent path. Pipeline auto-selects this when both
    # factories are wired (preferred). The legacy AuthAgent factory
    # above stays as a fallback for tests that mock it.
    def auth_planner_factory(architecture):
        return AuthPlanner(client=auth_client, on_status=on_status)

    def auth_operator_factory(architecture):
        fa_url, fa_key = _resolve_fa_endpoint(project_root)
        fa_orch = FusionAuthOrchestrator(
            base_url=fa_url,
            api_key=fa_key,
            on_status=on_status,
        )
        return FusionAuthOperator(
            orchestrator=fa_orch,
            on_status=on_status,
        )

    provisioner = Provisioner(
        project_parent=Path(os.environ.get("BIZNIZ_PROJECTS_ROOT") or
                             str(Path.home() / "bizniz_projects")),
        on_status_message=on_status,
    )

    def provision_callable(architecture, project_name):
        return provisioner.provision(architecture, project_name)

    pipeline = V2Pipeline(
        planner=planner,
        architect=architect,
        auth_agent_factory=auth_agent_factory,
        provision_callable=provision_callable,
        milestone_loop=milestone_loop,
        gates=gates,
        run_state=run_state,
        project_name=project_slug,
        compose_path_for_arch=lambda _arch: compose_path,
        cost_tracker=cost_tracker,
        on_status=on_status,
        auth_planner_factory=auth_planner_factory,
        auth_operator_factory=auth_operator_factory,
        auth_code_examples_client=auth_client,
        project_root=project_root,
    )
    # Attach cost tracker + run_state to the pipeline-build closure so
    # main() can write the cost report on exit.
    pipeline._build_runs_dir = run_state.root
    pipeline._build_cost_tracker = cost_tracker
    return pipeline


def main():
    p = argparse.ArgumentParser(prog="v2-build", description="v2 pipeline driver")
    p.add_argument("problem", nargs="?", help="Natural-language problem statement")
    p.add_argument("--project", required=True, help="Project slug (e.g. pet_groomer)")
    p.add_argument("--plan-only", action="store_true", help="Run only the Planner, then exit")
    p.add_argument("--milestone", type=int, default=None,
                   help="Run through milestone N (1-indexed, inclusive); default = all")
    p.add_argument("--resume", action="store_true",
                   help="Resume from the most recent job_id in this project's runs dir")
    p.add_argument("--resume-job-id", default=None,
                   help="Resume from a specific job_id (defaults to most recent)")
    p.add_argument("--auto", action="store_true",
                   help="Push through soft gates (warn + continue)")
    p.add_argument("--interactive", action="store_true",
                   help="Halt at every gate for human review")
    p.add_argument("--retry-failed", action="store_true",
                   help=(
                       "Reset every non-passed coder_issues row in the "
                       "given milestone back to pending so the implement "
                       "phase re-attempts them with current code/prompts. "
                       "Use after fixing a Coder/prompt bug to retry "
                       "issues without paying for ServicePlanner re-plan."
                   ))
    p.add_argument("--retry-service", default=None,
                   help="With --retry-failed, restrict the reset to one service")
    p.add_argument(
        "--decompose", action="store_true",
        help=(
            "Opt in to the Decomposer (per-unit dispatch). Default is "
            "OFF as of 2026-05-18 after two-fixture perf validation "
            "showed Decomposer is net-negative on Claude CLI "
            "(3-4× wall-clock penalty, no quality win). Use this flag "
            "for model-tier experiments or A/B comparison runs."
        ),
    )
    p.add_argument(
        "--use-v3-implement", action="store_true",
        help=(
            "Stage A of the v3 pipeline (shipped 2026-05-19). IMPLEMENT "
            "phase swaps from per-issue Coder loops to a single "
            "ServicePlannerWithScaffold + CoderAgentV3 dispatch per "
            "service. Anchor: Phase 2c validated 9 issues filled in "
            "7m 42s vs today's ~1h 35m for 12 issues. Use with "
            "--use-v3-review for the full v3 pipeline."
        ),
    )
    p.add_argument(
        "--use-v3-review", action="store_true",
        help=(
            "Stage B of the v3 pipeline (shipped 2026-05-19). Review/"
            "repair phase swaps from sequential QE → CR → "
            "ServicePlanner.repair → Coder × N loop to parallel "
            "ReviewUnitOrchestrator (QE + CR concurrent) + BatchFixDebugger "
            "consuming unified findings. ProgressTracker bounds the "
            "outer loop. Pairs with --use-v3-implement for the full "
            "v3 pipeline; can also be used standalone with v2 IMPLEMENT."
        ),
    )
    p.add_argument(
        "--use-v3", action="store_true",
        help=(
            "Shortcut for --use-v3-implement --use-v3-review. Enables "
            "the full v3 pipeline (Stage A + Stage B). DEPRECATED by "
            "--use-v3-1 (Stage B's UnifiedFinding adapter has a known "
            "approval-verdict bug; kept for archaeology only)."
        ),
    )
    p.add_argument(
        "--use-v3-1", dest="use_v3_1", action="store_true",
        help=(
            "v3.1 — V3 IMPLEMENT (Stage A) + parallel QE+CR review "
            "(V3 fan-out) + native CoverageReport/CodeReviewReport + "
            "V2 per-issue repair dispatch. Implies --use-v3-implement; "
            "does NOT enable Stage B's lossy UnifiedFinding adapter."
        ),
    )
    p.add_argument(
        "--use-v4", dest="use_v4", action="store_true",
        help=(
            "v4 — canonical path as of 2026-05-19. Per-issue "
            "CoderTesterAgent (one agent writes code AND tests) + "
            "PerIssueValidator (deterministic gates + fix-loop) + "
            "PIRunner (DAG-aware parallel ThreadPoolExecutor) for "
            "BOTH IMPLEMENT and REPAIR. Repair tier Opus-only (no "
            "Haiku escalation chain). Implies --use-v3-1 (parallel "
            "review + V2 approval semantics). max_parallel_coders "
            "from bizniz.yaml (default 6)."
        ),
    )
    p.add_argument(
        "--use-v5", dest="use_v5", action="store_true",
        help=(
            "v5 — canonical-findings monotone convergence. Iter 1 = "
            "full review frozen as CanonicalReport; iter 2+ = "
            "ResolutionChecker (no fresh review, no new findings "
            "invented). Regressions trigger ProjectGit rollback. "
            "Eliminates reviewer-drift as a regression source. "
            "Implies --use-v4."
        ),
    )
    p.add_argument(
        "--no-decompose", action="store_true",
        help=(
            "Deprecated explicit form of the new default (Decomposer "
            "off). Kept for backwards-compat with older invocations."
        ),
    )
    p.add_argument(
        "--phase",
        default=None,
        help=(
            "Run ONE phase only and exit. "
            "Top phases: plan|architect|provision|auth. "
            "Sub phases (require --milestone): "
            "enrich|implement|review_initial|review_final|"
            "repair_iter_0|repair_iter_1|repair_iter_2|"
            "integration_api|integration_web. "
            "Aliases: review→review_initial, repair→repair_iter_0."
        ),
    )
    args = p.parse_args()

    if not args.problem and not args.resume and not args.phase:
        p.error("provide a problem statement, --resume, or --phase")

    if args.resume and args.resume_job_id is None:
        runs_root = _resolve_runs_root(args.project)
        if not runs_root.exists():
            p.error(f"no prior runs at {runs_root}; cannot --resume")
        # Pick the most recent.
        candidates = sorted(
            (d for d in runs_root.iterdir() if d.is_dir()),
            key=lambda d: d.name, reverse=True,
        )
        if not candidates:
            p.error(f"no run directories in {runs_root}")
        args.resume_job_id = candidates[0].name

    on_status = _on_status()
    pipeline = _build_pipeline(args, on_status)

    on_status(f"v2-build: project={args.project} job={args.resume_job_id or 'new'} "
              f"mode={'auto' if args.auto else ('interactive' if args.interactive else 'strict')}")

    # --retry-failed: flip non-passed coder_issues rows to pending so
    # the implement phase re-attempts them. Useful after fixing a
    # Coder/prompt bug — saves ServicePlanner re-plan cost.
    if args.retry_failed:
        if args.milestone is None:
            p.error("--retry-failed requires --milestone")
        store = pipeline._milestone_loop._issue_store_factory(args.milestone)
        n = store.reset_non_passed_to_pending(service=args.retry_service)
        scope = (
            f"service={args.retry_service}"
            if args.retry_service else "all services"
        )
        on_status(
            f"--retry-failed: reset {n} issue(s) to pending "
            f"(milestone={args.milestone}, {scope})"
        )

    result = pipeline.run(
        problem_statement=args.problem or "",
        plan_only=args.plan_only,
        target_milestone=args.milestone,
        target_phase=args.phase,
    )

    # Finish the cost tracker job + write cost report.
    #
    # Two files:
    #   cost.md    — latest run only (overwritten each invocation; useful for
    #                quick eyeballing of "what did THIS gate cost")
    #   costs.md   — append-only log of every gate invocation against this
    #                project's runs dir (the documentation trail)
    tracker = getattr(pipeline, "_build_cost_tracker", None)
    runs_dir = getattr(pipeline, "_build_runs_dir", None)
    if tracker is not None:
        try:
            tracker.finish_job(
                status="halted" if result.halted_at else "succeeded",
            )
        except Exception:
            pass
        if runs_dir is not None:
            try:
                summary = tracker.summary()
                cost_md = runs_dir / "cost.md"
                cost_md.write_text(str(summary))

                # Append a dated entry to costs.md with per-call breakdown.
                phase_label = _phase_label_for_log(args)
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                halt_note = f" — HALTED@{result.halted_at}" if result.halted_at else ""

                # Per-call detail (one line per LLM call) — lets you
                # grep for "which agent burned the input tokens?"
                detail_lines: list[str] = []
                for rec in tracker.records():
                    detail_lines.append(
                        f"  {rec.agent:24s} {rec.model:32s} "
                        f"in={rec.input_tokens:>7,d} out={rec.output_tokens:>5,d}  "
                        f"${rec.cost.total_cost:.4f}"
                        + (f"  cached_in={rec.cached_input_tokens:,d}"
                           if getattr(rec, "cached_input_tokens", 0) else "")
                    )
                detail_block = "\n".join(detail_lines) if detail_lines else "  (no calls)"

                entry = (
                    f"## {ts} — {phase_label}{halt_note}\n\n"
                    f"{summary}\n\n"
                    f"### Per-call breakdown\n```\n{detail_block}\n```\n\n"
                    f"---\n\n"
                )
                costs_md = runs_dir / "costs.md"
                if costs_md.exists():
                    costs_md.write_text(costs_md.read_text() + entry)
                else:
                    costs_md.write_text("# Cost log\n\n" + entry)

                on_status(f"Cost report: {cost_md}; appended to {costs_md}")
            except Exception as e:
                on_status(f"Cost report write failed: {type(e).__name__}: {e}")

    if result.halted_at:
        on_status(f"HALTED at {result.halted_at}: {result.halt_reason}")
        sys.exit(1)
    on_status(f"DONE — {len(result.milestones_completed)} milestone(s) completed")
    sys.exit(0)


if __name__ == "__main__":
    main()

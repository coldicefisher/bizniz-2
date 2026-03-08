"""
Tests for AutoEngineer architecture planning and governance methods.
"""
import json
import pytest
from unittest.mock import MagicMock

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.engineer.auto_engineer import AutoEngineer
from bizniz.engineer.types import (
    ArchitecturePlan,
    ArchitectureNamespace,
    DomainModelDefinition,
    DomainModelField,
    MethodSignature,
    ModuleDefinition,
    DependencyEdge,
    DriftItem,
    GovernanceDecision,
    EngineeringAnalysis,
    EngineeringRequirement,
    EngineeringUseCase,
)
from bizniz.engineer.tests.conftest import (
    VALID_ANALYSIS_RESPONSE,
    VALID_PLAN_RESPONSE,
    make_ai_response,
)


RICH_PLAN_RESPONSE = {
    "package_name": "expense_tracker",
    "root_namespace": "expense_tracker",
    "namespaces": [
        {"namespace_path": "expense_tracker", "purpose": "Root package"},
        {"namespace_path": "expense_tracker/models", "purpose": "Domain models"},
        {"namespace_path": "expense_tracker/services", "purpose": "Business logic"},
    ],
    "domain_models": [
        {
            "class_name": "Expense",
            "filepath": "expense_tracker/models/expense.py",
            "namespace_path": "expense_tracker/models",
            "fields": [
                {"name": "amount", "type_hint": "float", "description": "The expense amount"},
                {"name": "category", "type_hint": "str", "description": "Expense category"},
            ],
            "methods": [
                {"name": "__init__", "signature": "def __init__(self, amount: float, category: str)", "description": "Constructor"},
            ],
            "docstring": "Represents an expense entry.",
        }
    ],
    "modules": [
        {
            "filepath": "expense_tracker/services/tracker.py",
            "class_name": "ExpenseTracker",
            "namespace_path": "expense_tracker/services",
            "methods": [
                {"name": "add", "signature": "def add(self, expense: Expense) -> None", "description": "Add an expense"},
                {"name": "total_by_category", "signature": "def total_by_category(self) -> dict", "description": "Sum by category"},
            ],
            "docstring": "Manages expense tracking.",
        }
    ],
    "dependencies": [
        {
            "source_filepath": "expense_tracker/services/tracker.py",
            "target_filepath": "expense_tracker/models/expense.py",
            "import_symbols": ["Expense"],
        }
    ],
}


@pytest.fixture
def ws(tmp_path):
    return BaseWorkspace(root=tmp_path)


@pytest.fixture
def mock_env():
    env = MagicMock(spec=BaseExecutionEnvironment)
    env.describe.return_value = "Test env"
    return env


class TestPlanArchitecture:

    def test_returns_architecture_plan(self, ws, mock_env):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = make_ai_response(RICH_PLAN_RESPONSE)

        eng = AutoEngineer(
            client=client,
            environment=mock_env,
            workspace=ws,
            orchestrator_factory=lambda: MagicMock(spec=CodingOrchestrator),
            max_retries=3,
        )

        # Create a problem + analysis first
        problem_id = ws.db.save_problem("Build expense tracker")
        analysis = EngineeringAnalysis(
            problem_id=problem_id,
            requirements=[
                EngineeringRequirement(db_id=1, type="business", text="Track expenses"),
            ],
            use_cases=[
                EngineeringUseCase(db_id=1, title="Add Expense", description="User adds an expense"),
            ],
        )

        plan = eng.plan_architecture(problem_id, analysis)

        assert isinstance(plan, ArchitecturePlan)
        assert plan.package_name == "expense_tracker"
        assert plan.root_namespace == "expense_tracker"
        assert plan.db_id is not None

    def test_persists_namespaces(self, ws, mock_env):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = make_ai_response(RICH_PLAN_RESPONSE)

        eng = AutoEngineer(
            client=client, environment=mock_env, workspace=ws,
            orchestrator_factory=lambda: MagicMock(spec=CodingOrchestrator),
            max_retries=3,
        )

        problem_id = ws.db.save_problem("Build expense tracker")
        analysis = EngineeringAnalysis(problem_id=problem_id)
        plan = eng.plan_architecture(problem_id, analysis)

        assert len(plan.namespaces) == 3
        ns_paths = [ns.namespace_path for ns in plan.namespaces]
        assert "expense_tracker/models" in ns_paths
        assert "expense_tracker/services" in ns_paths

        # Check DB persistence
        db_namespaces = ws.db.get_namespaces(plan.db_id)
        assert len(db_namespaces) == 3

    def test_persists_domain_models(self, ws, mock_env):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = make_ai_response(RICH_PLAN_RESPONSE)

        eng = AutoEngineer(
            client=client, environment=mock_env, workspace=ws,
            orchestrator_factory=lambda: MagicMock(spec=CodingOrchestrator),
            max_retries=3,
        )

        problem_id = ws.db.save_problem("Build expense tracker")
        analysis = EngineeringAnalysis(problem_id=problem_id)
        plan = eng.plan_architecture(problem_id, analysis)

        assert len(plan.domain_models) == 1
        dm = plan.domain_models[0]
        assert dm.class_name == "Expense"
        assert len(dm.fields) == 2
        assert dm.fields[0].name == "amount"

        # Check DB persistence
        db_models = ws.db.get_domain_models(plan.db_id)
        assert len(db_models) == 1

    def test_persists_modules(self, ws, mock_env):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = make_ai_response(RICH_PLAN_RESPONSE)

        eng = AutoEngineer(
            client=client, environment=mock_env, workspace=ws,
            orchestrator_factory=lambda: MagicMock(spec=CodingOrchestrator),
            max_retries=3,
        )

        problem_id = ws.db.save_problem("Build expense tracker")
        analysis = EngineeringAnalysis(problem_id=problem_id)
        plan = eng.plan_architecture(problem_id, analysis)

        assert len(plan.modules) == 1
        assert plan.modules[0].class_name == "ExpenseTracker"

    def test_persists_dependencies(self, ws, mock_env):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = make_ai_response(RICH_PLAN_RESPONSE)

        eng = AutoEngineer(
            client=client, environment=mock_env, workspace=ws,
            orchestrator_factory=lambda: MagicMock(spec=CodingOrchestrator),
            max_retries=3,
        )

        problem_id = ws.db.save_problem("Build expense tracker")
        analysis = EngineeringAnalysis(problem_id=problem_id)
        plan = eng.plan_architecture(problem_id, analysis)

        assert len(plan.dependencies) == 1
        dep = plan.dependencies[0]
        assert dep.source_filepath == "expense_tracker/services/tracker.py"
        assert dep.target_filepath == "expense_tracker/models/expense.py"
        assert "Expense" in dep.import_symbols


class TestCreatePackageStructure:

    def test_creates_package_directory(self, ws, mock_env):
        eng = AutoEngineer(
            client=MagicMock(spec=BaseAIClient),
            environment=mock_env,
            workspace=ws,
            orchestrator_factory=lambda: MagicMock(spec=CodingOrchestrator),
            max_retries=3,
        )

        plan = ArchitecturePlan(
            problem_id=1,
            package_name="expense_tracker",
            root_namespace="expense_tracker",
            namespaces=[
                ArchitectureNamespace(namespace_path="expense_tracker", purpose="Root"),
                ArchitectureNamespace(namespace_path="expense_tracker/models", purpose="Models"),
            ],
        )

        eng.create_package_structure(plan)

        assert (ws.root / "expense_tracker" / "__init__.py").exists()
        assert (ws.root / "expense_tracker" / "models" / "__init__.py").exists()
        assert (ws.root / "pyproject.toml").exists()
        assert (ws.root / "tests" / "__init__.py").exists()


class TestFormatArchitectureContext:

    def test_includes_package_name(self):
        plan = ArchitecturePlan(
            problem_id=1,
            package_name="myapp",
            root_namespace="myapp",
        )
        ctx = AutoEngineer.format_architecture_context(plan)
        assert "myapp" in ctx

    def test_includes_namespaces(self):
        plan = ArchitecturePlan(
            problem_id=1,
            package_name="myapp",
            root_namespace="myapp",
            namespaces=[
                ArchitectureNamespace(namespace_path="myapp/models", purpose="Domain models"),
            ],
        )
        ctx = AutoEngineer.format_architecture_context(plan)
        assert "myapp/models" in ctx
        assert "Domain models" in ctx

    def test_includes_domain_models(self):
        plan = ArchitecturePlan(
            problem_id=1,
            package_name="myapp",
            root_namespace="myapp",
            domain_models=[
                DomainModelDefinition(
                    class_name="User",
                    filepath="myapp/models/user.py",
                    fields=[DomainModelField(name="name", type_hint="str", description="User name")],
                ),
            ],
        )
        ctx = AutoEngineer.format_architecture_context(plan)
        assert "User" in ctx
        assert "name: str" in ctx

    def test_includes_dependencies(self):
        plan = ArchitecturePlan(
            problem_id=1,
            package_name="myapp",
            root_namespace="myapp",
            dependencies=[
                DependencyEdge(
                    source_filepath="myapp/services/auth.py",
                    target_filepath="myapp/models/user.py",
                    import_symbols=["User"],
                ),
            ],
        )
        ctx = AutoEngineer.format_architecture_context(plan)
        assert "myapp/services/auth.py" in ctx
        assert "myapp/models/user.py" in ctx
        assert "User" in ctx


class TestReviewDrift:

    def test_returns_governance_decision(self, ws, mock_env):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = make_ai_response({
            "decision": "approve",
            "reason": "The utility helper is a reasonable addition.",
            "plan_updates": "",
        })

        eng = AutoEngineer(
            client=client, environment=mock_env, workspace=ws,
            orchestrator_factory=lambda: MagicMock(spec=CodingOrchestrator),
            max_retries=3,
        )

        plan = ArchitecturePlan(
            problem_id=1,
            package_name="myapp",
            root_namespace="myapp",
        )

        drift_items = [
            DriftItem(filepath="myapp/utils.py", drift_type="unplanned_file", reason="Created by autocoder"),
        ]

        decision = eng.review_drift(plan, drift_items)

        assert isinstance(decision, GovernanceDecision)
        assert decision.decision == "approve"
        assert "reasonable" in decision.reason

    def test_reject_decision(self, ws, mock_env):
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = make_ai_response({
            "decision": "reject",
            "reason": "This introduces circular dependencies.",
            "plan_updates": "",
        })

        eng = AutoEngineer(
            client=client, environment=mock_env, workspace=ws,
            orchestrator_factory=lambda: MagicMock(spec=CodingOrchestrator),
            max_retries=3,
        )

        plan = ArchitecturePlan(problem_id=1, package_name="myapp", root_namespace="myapp")
        drift_items = [DriftItem(filepath="myapp/bad.py", reason="Bad module")]

        decision = eng.review_drift(plan, drift_items)
        assert decision.decision == "reject"

    def test_modify_updates_plan_in_db(self, ws, mock_env):
        plan_updates = json.dumps({
            "namespaces": [{"namespace_path": "myapp/utils", "purpose": "Utilities"}],
        })
        client = MagicMock(spec=BaseAIClient)
        client.get_text.return_value = make_ai_response({
            "decision": "modify",
            "reason": "Good idea, adding utils namespace.",
            "plan_updates": plan_updates,
        })

        eng = AutoEngineer(
            client=client, environment=mock_env, workspace=ws,
            orchestrator_factory=lambda: MagicMock(spec=CodingOrchestrator),
            max_retries=3,
        )

        # Create a plan in DB first
        problem_id = ws.db.save_problem("test")
        plan_id = ws.db.save_architecture_plan(
            problem_id=problem_id,
            package_name="myapp",
            root_namespace="myapp",
            plan_json=json.dumps({"package_name": "myapp", "root_namespace": "myapp", "namespaces": [], "domain_models": [], "modules": [], "dependencies": []}),
        )

        plan = ArchitecturePlan(
            db_id=plan_id,
            problem_id=problem_id,
            package_name="myapp",
            root_namespace="myapp",
        )
        drift_items = [DriftItem(filepath="myapp/utils.py", reason="New utility")]

        decision = eng.review_drift(plan, drift_items)
        assert decision.decision == "modify"

        # Verify plan was updated in DB
        updated = ws.db.get_architecture_plan(problem_id)
        updated_data = json.loads(updated["plan_json"])
        ns_paths = [ns["namespace_path"] for ns in updated_data.get("namespaces", [])]
        assert "myapp/utils" in ns_paths

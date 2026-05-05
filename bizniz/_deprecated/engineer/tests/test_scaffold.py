"""Tests for the scaffold generator."""
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from bizniz.engineer.scaffold import scaffold_from_plan, _filepath_to_module
from bizniz.engineer.types import (
    ArchitecturePlan,
    ArchitectureNamespace,
    DomainModelDefinition,
    DomainModelField,
    MethodSignature,
    ModuleDefinition,
    DependencyEdge,
    EngineeringIssue,
    TargetFile,
)


def _make_plan():
    """Build a minimal pet_groomer-style architecture plan."""
    return ArchitecturePlan(
        problem_id=1,
        package_name="pet_groomer",
        root_namespace="pet_groomer",
        namespaces=[
            ArchitectureNamespace(namespace_path="pet_groomer/models", purpose="Domain models"),
            ArchitectureNamespace(namespace_path="pet_groomer/routers", purpose="API routers"),
        ],
        domain_models=[
            DomainModelDefinition(
                class_name="Service",
                filepath="pet_groomer/models/service.py",
                namespace_path="pet_groomer/models",
                fields=[
                    DomainModelField(name="name", type_hint="str", description="Service name"),
                    DomainModelField(name="price", type_hint="float", description="Price"),
                ],
                methods=[],
                docstring="Grooming service model.",
            ),
        ],
        modules=[
            ModuleDefinition(
                filepath="pet_groomer/routers/services.py",
                class_name="",
                namespace_path="pet_groomer/routers",
                methods=[
                    MethodSignature(
                        name="list_services",
                        signature="def list_services() -> list",
                        description="List all services",
                    ),
                ],
                docstring="Services router.",
            ),
        ],
        dependencies=[
            DependencyEdge(
                source_filepath="pet_groomer/routers/services.py",
                target_filepath="pet_groomer/models/service.py",
                import_symbols=["Service"],
            ),
        ],
    )


def _make_issues():
    return [
        EngineeringIssue(
            db_id=1,
            title="Create Service Model",
            description="Define the Service domain model.",
            target_files=[TargetFile(filepath="pet_groomer/models/service.py", action="create")],
            test_files=["tests/test_service_model.py"],
        ),
        EngineeringIssue(
            db_id=2,
            title="Build Services Router",
            description="Implement the services API.",
            target_files=[TargetFile(filepath="pet_groomer/routers/services.py", action="create")],
            test_files=["tests/test_services_router.py"],
            depends_on_titles=["Create Service Model"],
            test_setup_hint="Use TestClient(create_app()) from pet_groomer.app",
        ),
    ]


class TestScaffoldFromPlan:
    def test_creates_stub_files(self, tmp_path):
        workspace = MagicMock()
        workspace.root = tmp_path

        # Create the package root so write_file can work
        (tmp_path / "pet_groomer" / "models").mkdir(parents=True)
        (tmp_path / "pet_groomer" / "routers").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)

        def write_file(filepath, content):
            path = tmp_path / filepath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        def exists(path):
            return (tmp_path / path).exists()

        workspace.write_file = write_file
        workspace.exists = exists

        plan = _make_plan()
        issues = _make_issues()

        result = scaffold_from_plan(workspace, plan, issues)

        # Should create source stubs + test stubs
        assert "pet_groomer/models/service.py" in result
        assert "pet_groomer/routers/services.py" in result
        assert "tests/test_service_model.py" in result
        assert "tests/test_services_router.py" in result

    def test_domain_model_has_class_and_fields(self, tmp_path):
        workspace = MagicMock()
        workspace.root = tmp_path
        (tmp_path / "pet_groomer" / "models").mkdir(parents=True)
        (tmp_path / "pet_groomer" / "routers").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)

        def write_file(filepath, content):
            (tmp_path / filepath).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / filepath).write_text(content)

        workspace.write_file = write_file
        workspace.exists = lambda path: (tmp_path / path).exists()

        plan = _make_plan()
        issues = _make_issues()
        result = scaffold_from_plan(workspace, plan, issues)

        model_content = result["pet_groomer/models/service.py"]
        assert "class Service" in model_content
        assert "name: str" in model_content
        assert "price: float" in model_content

    def test_module_stub_has_imports_from_dependency_graph(self, tmp_path):
        workspace = MagicMock()
        workspace.root = tmp_path
        (tmp_path / "pet_groomer" / "models").mkdir(parents=True)
        (tmp_path / "pet_groomer" / "routers").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)

        def write_file(filepath, content):
            (tmp_path / filepath).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / filepath).write_text(content)

        workspace.write_file = write_file
        workspace.exists = lambda path: (tmp_path / path).exists()

        plan = _make_plan()
        issues = _make_issues()
        result = scaffold_from_plan(workspace, plan, issues)

        router_content = result["pet_groomer/routers/services.py"]
        assert "from pet_groomer.models.service import Service" in router_content

    def test_test_stub_imports_target(self, tmp_path):
        workspace = MagicMock()
        workspace.root = tmp_path
        (tmp_path / "pet_groomer" / "models").mkdir(parents=True)
        (tmp_path / "pet_groomer" / "routers").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)

        def write_file(filepath, content):
            (tmp_path / filepath).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / filepath).write_text(content)

        workspace.write_file = write_file
        workspace.exists = lambda path: (tmp_path / path).exists()

        plan = _make_plan()
        issues = _make_issues()
        result = scaffold_from_plan(workspace, plan, issues)

        test_content = result["tests/test_service_model.py"]
        assert "import pytest" in test_content
        assert "from pet_groomer.models.service import Service" in test_content

    def test_flips_create_to_modify(self, tmp_path):
        workspace = MagicMock()
        workspace.root = tmp_path
        (tmp_path / "pet_groomer" / "models").mkdir(parents=True)
        (tmp_path / "pet_groomer" / "routers").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)

        def write_file(filepath, content):
            (tmp_path / filepath).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / filepath).write_text(content)

        workspace.write_file = write_file
        workspace.exists = lambda path: (tmp_path / path).exists()

        plan = _make_plan()
        issues = _make_issues()

        assert issues[0].target_files[0].action == "create"
        scaffold_from_plan(workspace, plan, issues)
        assert issues[0].target_files[0].action == "modify"

    def test_test_setup_hint_in_test_stub(self, tmp_path):
        workspace = MagicMock()
        workspace.root = tmp_path
        (tmp_path / "pet_groomer" / "models").mkdir(parents=True)
        (tmp_path / "pet_groomer" / "routers").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)

        def write_file(filepath, content):
            (tmp_path / filepath).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / filepath).write_text(content)

        workspace.write_file = write_file
        workspace.exists = lambda path: (tmp_path / path).exists()

        plan = _make_plan()
        issues = _make_issues()
        result = scaffold_from_plan(workspace, plan, issues)

        router_test = result["tests/test_services_router.py"]
        assert "TestClient" in router_test


class TestFilepathToModule:
    def test_simple(self):
        assert _filepath_to_module("pet_groomer/models/service.py") == "pet_groomer.models.service"

    def test_init(self):
        assert _filepath_to_module("pet_groomer/__init__.py") == "pet_groomer"

    def test_no_extension(self):
        assert _filepath_to_module("pet_groomer/models") == "pet_groomer.models"

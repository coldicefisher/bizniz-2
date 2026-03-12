"""
Tests for CodingOrchestrator.run_multi() — multi-file orchestration loop.
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from bizniz.autocoder.autocoder import Autocoder
from bizniz.autotester.autotester import Autotester
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import (
    ExecutionEnvironmentResult,
    ExecutionEnvironmentErrorDetails,
)
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.autocoder.types import AutocoderProcessResult, FileChange
from bizniz.autotester.types import AutotesterResult, GeneratedTestFile
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.orchestrator.strategy import CodingStrategy
from bizniz.orchestrator.types import OrchestratorMaxIterationsError


# ── Fixtures ──────────────────────────────────────────────────────────────────

CODE_MODELS = "class Expense:\n    def __init__(self, amount): self.amount = amount\n"
CODE_CLI = "from .models import Expense\ndef main(): pass\n"
TESTS_MODELS = "import pytest\ndef test_expense():\n    from pkg.models import Expense\n    assert Expense(10).amount == 10\n"
TESTS_CLI = "import pytest\ndef test_main():\n    from pkg.cli import main\n    assert main() is None\n"


def _autocoder_generate_side_effect(**kwargs):
    """Return per-file results based on which target file is requested."""
    target_files = kwargs.get("target_files", [])
    changes = []
    for tf in target_files:
        fp = tf["filepath"]
        if fp == "pkg/models.py":
            changes.append(FileChange(filepath=fp, code=CODE_MODELS, action="create"))
        elif fp == "pkg/cli.py":
            changes.append(FileChange(filepath=fp, code=CODE_CLI, action="create"))
        else:
            changes.append(FileChange(filepath=fp, code="# generated\n", action="create"))
    return AutocoderProcessResult(changes=changes)


def _autotester_generate_side_effect(**kwargs):
    """Return per-file results based on which test file is requested."""
    test_files = kwargs.get("test_files", [])
    result_files = []
    for tf in test_files:
        if tf == "tests/test_models.py":
            result_files.append(GeneratedTestFile(filepath=tf, tests=TESTS_MODELS))
        elif tf == "tests/test_cli.py":
            result_files.append(GeneratedTestFile(filepath=tf, tests=TESTS_CLI))
        else:
            result_files.append(GeneratedTestFile(filepath=tf, tests="def test_placeholder(): pass\n"))
    return AutotesterResult(test_files=result_files, mode="from_prompt", success=True)


@pytest.fixture
def mock_autocoder():
    ac = MagicMock(spec=Autocoder)
    ac.generate_multi.side_effect = _autocoder_generate_side_effect
    ac.repair_multi.return_value = AutocoderProcessResult(
        changes=[
            FileChange(filepath="pkg/models.py", code=CODE_MODELS + "# fixed\n", action="modify"),
        ]
    )
    ac.repair_multi_inline.return_value = AutocoderProcessResult(
        changes=[
            FileChange(filepath="pkg/models.py", code=CODE_MODELS + "# fixed\n", action="modify"),
        ],
        dependencies=[],
    )
    return ac


@pytest.fixture
def mock_autotester():
    at = MagicMock(spec=Autotester)
    at.generate_multi.side_effect = _autotester_generate_side_effect
    return at


@pytest.fixture
def mock_test_env():
    env = MagicMock(spec=BaseExecutionEnvironment)
    env.execute.return_value = ExecutionEnvironmentResult(success=True)
    return env


@pytest.fixture
def mock_workspace(tmp_path):
    ws = MagicMock(spec=BaseWorkspace)
    ws.path.side_effect = lambda p: tmp_path / p
    ws.exists.return_value = False
    ws.read_file.return_value = ""
    ws.list_relative_files.return_value = []
    return ws


@pytest.fixture
def orchestrator(mock_autocoder, mock_autotester, mock_test_env, mock_workspace):
    return CodingOrchestrator(
        autocoder=mock_autocoder,
        autotester=mock_autotester,
        test_environment=mock_test_env,
        workspace=mock_workspace,
        max_iterations=5,
    )


TARGET_FILES = [
    {"filepath": "pkg/models.py", "action": "create"},
    {"filepath": "pkg/cli.py", "action": "create"},
]
TEST_FILES = ["tests/test_models.py", "tests/test_cli.py"]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunMultiSuccess:

    def test_returns_success_when_tests_pass(self, orchestrator):
        result = orchestrator.run_multi(
            prompt="Build expense tracker",
            target_files=TARGET_FILES,
            test_files=TEST_FILES,
        )

        assert result.success is True
        assert result.iterations == 1
        assert len(result.changes) >= 2  # May include auto-stubbed __init__.py
        assert len(result.test_files) == 2

    def test_calls_generate_multi_per_file(self, orchestrator, mock_autocoder):
        orchestrator.run_multi(
            prompt="Build expense tracker",
            target_files=TARGET_FILES,
            test_files=TEST_FILES,
            architecture_context="Package: pkg",
        )

        # Per-file generation: 2 target files = 2 code gen calls
        assert mock_autocoder.generate_multi.call_count == 2
        # Each call should have one target file and include the prompt
        for c in mock_autocoder.generate_multi.call_args_list:
            kwargs = c[1]
            assert len(kwargs["target_files"]) == 1
            assert "Build expense tracker" in kwargs["issue_description"]
            assert kwargs["architecture_context"] == "Package: pkg"

    def test_calls_generate_multi_tests_per_file(self, orchestrator, mock_autotester):
        orchestrator.run_multi(
            prompt="Build expense tracker",
            target_files=TARGET_FILES,
            test_files=TEST_FILES,
        )

        # Per-file generation: 2 test files = 2 test gen calls
        assert mock_autotester.generate_multi.call_count == 2
        for c in mock_autotester.generate_multi.call_args_list:
            kwargs = c[1]
            assert len(kwargs["test_files"]) == 1
            # In TDD mode, tests are generated without source code
            assert kwargs["source_code"] is None

    def test_loads_existing_code_for_modify_actions(self, orchestrator, mock_workspace, mock_autocoder):
        mock_workspace.exists.side_effect = lambda path: path == "pkg/models.py"
        mock_workspace.read_file.return_value = "# existing code"

        target_files = [
            {"filepath": "pkg/models.py", "action": "modify"},
            {"filepath": "pkg/cli.py", "action": "create"},
        ]

        orchestrator.run_multi(
            prompt="Update models",
            target_files=target_files,
            test_files=TEST_FILES,
        )

        # The models.py call should include existing_code for that file
        models_call = [c for c in mock_autocoder.generate_multi.call_args_list
                       if c[1]["target_files"][0]["filepath"] == "pkg/models.py"][0]
        assert "pkg/models.py" in models_call[1]["existing_code"]
        assert models_call[1]["existing_code"]["pkg/models.py"] == "# existing code"


class TestRunMultiRepair:

    def test_repairs_on_failure(self, orchestrator, mock_test_env, mock_autocoder):
        # First call fails, second passes
        mock_test_env.execute.side_effect = [
            ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    type="TestFailure",
                    message="pytest exited with code 1",
                    traceback="FAILED test_models.py::test_expense",
                ),
                stdout="FAILED test_models.py::test_expense",
            ),
            ExecutionEnvironmentResult(success=True),
        ]

        result = orchestrator.run_multi(
            prompt="Build tracker",
            target_files=TARGET_FILES,
            test_files=TEST_FILES,
        )

        assert result.success is True
        assert result.iterations == 2
        mock_autocoder.repair_multi_inline.assert_called_once()

    def test_raises_after_max_iterations(self, orchestrator, mock_test_env):
        mock_test_env.execute.return_value = ExecutionEnvironmentResult(
            success=False,
            error=ExecutionEnvironmentErrorDetails(
                type="TestFailure",
                message="pytest exited with code 1",
                traceback="FAILED",
            ),
            stdout="FAILED",
        )

        with pytest.raises(OrchestratorMaxIterationsError):
            orchestrator.run_multi(
                prompt="Build tracker",
                target_files=TARGET_FILES,
                test_files=TEST_FILES,
            )


class TestRunMultiMissingPackage:

    def test_installs_missing_package(self, orchestrator, mock_test_env, mock_workspace):
        mock_workspace.list_relative_files.return_value = []

        mock_test_env.execute.side_effect = [
            ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    type="TestFailure",
                    message="pytest exited with code 1",
                    traceback="ModuleNotFoundError: No module named 'requests'",
                ),
                stdout="ModuleNotFoundError: No module named 'requests'",
            ),
            ExecutionEnvironmentResult(success=True),
        ]

        result = orchestrator.run_multi(
            prompt="Build tracker",
            target_files=TARGET_FILES,
            test_files=TEST_FILES,
        )

        assert result.success is True


class TestRunMultiCollectionError:

    def test_regenerates_tests_on_collection_error(self, orchestrator, mock_test_env, mock_autotester):
        mock_test_env.execute.side_effect = [
            ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    type="TestFailure",
                    message="pytest exited with code 2",
                    traceback="E   fixture 'foo' not found",
                ),
                stdout="E   fixture 'foo' not found",
            ),
            ExecutionEnvironmentResult(success=True),
        ]

        result = orchestrator.run_multi(
            prompt="Build tracker",
            target_files=TARGET_FILES,
            test_files=TEST_FILES,
        )

        assert result.success is True
        # generate_multi called: 2 initial per-file + at least 1 regeneration
        assert mock_autotester.generate_multi.call_count >= 3


class TestDriftDetection:

    def test_detects_unplanned_files(self):
        planned = [{"filepath": "pkg/models.py", "action": "create"}]
        actual = [
            FileChange(filepath="pkg/models.py", code="...", action="create"),
            FileChange(filepath="pkg/utils.py", code="...", action="create"),
        ]

        drift = CodingOrchestrator._detect_drift(planned, actual)
        assert drift == ["pkg/utils.py"]

    def test_no_drift_when_all_planned(self):
        planned = [
            {"filepath": "pkg/models.py", "action": "create"},
            {"filepath": "pkg/cli.py", "action": "create"},
        ]
        actual = [
            FileChange(filepath="pkg/models.py", code="...", action="create"),
            FileChange(filepath="pkg/cli.py", code="...", action="create"),
        ]

        drift = CodingOrchestrator._detect_drift(planned, actual)
        assert drift == []

    def test_drift_flag_set_in_result(self, orchestrator, mock_autocoder):
        # Autocoder creates an unplanned file alongside the planned one
        def drift_side_effect(**kwargs):
            return AutocoderProcessResult(
                changes=[
                    FileChange(filepath="pkg/models.py", code=CODE_MODELS, action="create"),
                    FileChange(filepath="pkg/utils.py", code="# unplanned", action="create"),
                ]
            )
        mock_autocoder.generate_multi.side_effect = drift_side_effect

        result = orchestrator.run_multi(
            prompt="Build tracker",
            target_files=[{"filepath": "pkg/models.py", "action": "create"}],
            test_files=TEST_FILES,
        )

        assert result.architecture_drift_detected is True


class TestRegressionDetection:

    def test_detects_regression(self, mock_autocoder, mock_autotester, mock_workspace, tmp_path):
        # Create a real test file on disk so Path.exists() passes
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        existing_test = test_dir / "test_existing.py"
        existing_test.write_text("def test_ok(): pass\n")

        mock_workspace.list_relative_files.return_value = [
            Path("tests/test_existing.py"),
        ]
        mock_workspace.path.side_effect = lambda p: tmp_path / p

        mock_test_env = MagicMock(spec=BaseExecutionEnvironment)

        # Calls: 1=baseline passes, 2=issue tests pass, 3=regression check fails,
        # 4=issue tests pass again, 5=regression check passes
        call_count = [0]
        def execute_side_effect(code="", call_spec=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return ExecutionEnvironmentResult(success=True)  # baseline
            elif call_count[0] == 2:
                return ExecutionEnvironmentResult(success=True)  # issue tests pass
            elif call_count[0] == 3:
                return ExecutionEnvironmentResult(  # regression detected
                    success=False,
                    error=ExecutionEnvironmentErrorDetails(
                        type="TestFailure",
                        message="pytest exited with code 1",
                        traceback="FAILED",
                    ),
                )
            else:
                return ExecutionEnvironmentResult(success=True)  # all pass after repair

        mock_test_env.execute.side_effect = execute_side_effect

        orch = CodingOrchestrator(
            autocoder=mock_autocoder,
            autotester=mock_autotester,
            test_environment=mock_test_env,
            workspace=mock_workspace,
            max_iterations=5,
        )

        result = orch.run_multi(
            prompt="Build tracker",
            target_files=TARGET_FILES,
            test_files=TEST_FILES,
        )

        assert mock_autocoder.repair_multi_inline.call_count >= 1

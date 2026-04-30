import pytest
from bizniz.engineer.types import (
    EngineeringAnalysis,
    EngineeringRequirement,
    EngineeringUseCase,
    EngineeringIssue,
    ArchitecturePlan,
    EngineerBadAIResponseError,
)

PROBLEM = "Build a task management system."


def test_analyze_returns_engineering_analysis(engineer):
    result = engineer.analyze(PROBLEM)
    assert isinstance(result, EngineeringAnalysis)


def test_analyze_persists_problem(engineer, tmp_path):
    result = engineer.analyze(PROBLEM)
    assert result.problem_id is not None
    assert result.problem_id > 0


def test_analyze_populates_requirements(engineer):
    result = engineer.analyze(PROBLEM)
    assert len(result.requirements) > 0


def test_analyze_populates_use_cases(engineer):
    result = engineer.analyze(PROBLEM)
    assert len(result.use_cases) > 0
    for uc in result.use_cases:
        assert isinstance(uc, EngineeringUseCase)
        assert uc.title
        assert uc.description


def test_analyze_populates_issues(engineer):
    result = engineer.analyze(PROBLEM)
    assert len(result.issues) > 0
    for issue in result.issues:
        assert isinstance(issue, EngineeringIssue)
        assert len(issue.target_files) > 0
        assert all(tf.filepath.endswith(".py") for tf in issue.target_files)
        assert len(issue.test_files) > 0
        assert all(tf.endswith(".py") for tf in issue.test_files)
        assert issue.db_id is not None


def test_analyze_requirements_have_db_ids(engineer):
    result = engineer.analyze(PROBLEM)
    for req in result.requirements:
        assert req.db_id is not None


def test_analyze_includes_architecture_plan(engineer):
    result = engineer.analyze(PROBLEM)
    assert result.architecture is not None
    assert isinstance(result.architecture, ArchitecturePlan)
    assert result.architecture.package_name == "task_manager"
    assert len(result.architecture.namespaces) > 0


def test_analyze_calls_ai_multiple_times(engineer, mock_client):
    """analyze() makes 3 AI calls: analysis, architecture plan, refined analysis."""
    engineer.analyze(PROBLEM)
    assert mock_client.get_text.call_count == 3


def test_analyze_creates_package_structure(engineer, mock_workspace):
    engineer.analyze(PROBLEM)
    # The package directory should have been created
    assert (mock_workspace.root / "task_manager" / "__init__.py").exists()
    assert (mock_workspace.root / "pyproject.toml").exists()


def test_analyze_raises_on_bad_ai_response(mock_client, mock_environment, mock_orchestrator, tmp_path):
    from bizniz.engineer.engineer import Engineer
    from bizniz.workspace.base_workspace import BaseWorkspace

    ws = BaseWorkspace(root=tmp_path)
    mock_client.get_text.side_effect = Exception("Network error")

    eng = Engineer(
        client=mock_client,
        environment=mock_environment,
        workspace=ws,
        orchestrator_factory=lambda: mock_orchestrator,
        max_retries=2,
    )
    with pytest.raises(EngineerBadAIResponseError):
        eng.analyze(PROBLEM)

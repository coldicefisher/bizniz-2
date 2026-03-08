import pytest
from bizniz.engineer.types import (
    EngineeringAnalysis,
    EngineeringRequirement,
    EngineeringUseCase,
    EngineeringIssue,
    AutoEngineerBadAIResponseError,
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
        assert issue.code_file.endswith(".py")
        assert issue.test_file.endswith(".py")
        assert issue.db_id is not None


def test_analyze_requirements_have_db_ids(engineer):
    result = engineer.analyze(PROBLEM)
    for req in result.requirements:
        assert req.db_id is not None


def test_analyze_calls_ai(engineer, mock_client):
    engineer.analyze(PROBLEM)
    mock_client.get_text.assert_called_once()


def test_analyze_raises_on_bad_ai_response(mock_client, mock_environment, mock_workspace, mock_orchestrator, tmp_path):
    from bizniz.engineer.auto_engineer import AutoEngineer

    mock_workspace.root = tmp_path
    mock_client.get_text.side_effect = Exception("Network error")

    eng = AutoEngineer(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
        orchestrator_factory=lambda: mock_orchestrator,
        max_retries=2,
    )
    with pytest.raises(AutoEngineerBadAIResponseError):
        eng.analyze(PROBLEM)

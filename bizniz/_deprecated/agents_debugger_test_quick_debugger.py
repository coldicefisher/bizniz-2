import json
import pytest
from unittest.mock import MagicMock

from bizniz.agents.debugger.quick import QuickDebugger
from bizniz.agents.debugger.types import QuickDebuggerDiagnosis, QuickDebuggerBadAIResponseError


def _make_ai_response(diagnosis, fix_target, relevant_files, suggested_approach):
    """Build a mock AI response tuple."""
    # Convert dict to array format matching the schema
    if isinstance(relevant_files, dict):
        files_array = [{"filename": k, "summary": v} for k, v in relevant_files.items()]
    else:
        files_array = relevant_files
    text = json.dumps({
        "diagnosis": diagnosis,
        "fix_target": fix_target,
        "relevant_files": files_array,
        "suggested_approach": suggested_approach,
    })
    return text, "job-123", [{"role": "assistant", "content": text}]


def test_diagnose_returns_diagnosis(mock_client, mock_environment, mock_workspace):
    mock_client.get_text.return_value = _make_ai_response(
        diagnosis="Tests import from wrong module",
        fix_target="tests",
        relevant_files={"add_expense.py": "Defines ExpenseTracker class"},
        suggested_approach="Change import to use add_expense module",
    )

    debugger = QuickDebugger(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
    )

    result = debugger.diagnose(
        error_output="ImportError: cannot import 'ExpenseTracker' from 'list_expenses'",
        code="class ExpenseTracker:\n    pass\n",
        code_filename="list_expenses.py",
        test_code="from list_expenses import ExpenseTracker\n",
        test_filename="test_list_expenses.py",
    )

    assert isinstance(result, QuickDebuggerDiagnosis)
    assert result.fix_target == "tests"
    assert "wrong module" in result.diagnosis
    assert "add_expense.py" in result.relevant_files


def test_diagnose_fix_target_code(mock_client, mock_environment, mock_workspace):
    mock_client.get_text.return_value = _make_ai_response(
        diagnosis="list_expenses method returns None instead of formatted string",
        fix_target="code",
        relevant_files={},
        suggested_approach="Implement list_expenses to return formatted output",
    )

    debugger = QuickDebugger(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
    )

    result = debugger.diagnose(
        error_output="AssertionError: None != 'Category: Food, Amount: 50.25'",
        code="class ExpenseTracker:\n    def list_expenses(self): pass\n",
        code_filename="expense_tracker.py",
        test_code="def test_list(): ...\n",
        test_filename="test_expense_tracker.py",
    )

    assert result.fix_target == "code"


def test_diagnose_retries_on_empty_response(mock_client, mock_environment, mock_workspace):
    good_response = _make_ai_response(
        diagnosis="Missing function",
        fix_target="code",
        relevant_files={},
        suggested_approach="Add the function",
    )
    mock_client.get_text.side_effect = [
        ("", "job-1", [{"role": "assistant", "content": ""}]),
        good_response,
    ]

    debugger = QuickDebugger(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
    )

    result = debugger.diagnose(
        error_output="NameError",
        code="pass",
        code_filename="foo.py",
        test_code="pass",
        test_filename="test_foo.py",
    )

    assert result.fix_target == "code"
    assert mock_client.get_text.call_count == 2


def test_diagnose_raises_after_max_retries(mock_client, mock_environment, mock_workspace):
    mock_client.get_text.return_value = ("", "job-1", [{"role": "assistant", "content": ""}])

    debugger = QuickDebugger(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
    )

    with pytest.raises(QuickDebuggerBadAIResponseError):
        debugger.diagnose(
            error_output="error",
            code="pass",
            code_filename="foo.py",
            test_code="pass",
            test_filename="test_foo.py",
        )


def test_find_related_files_from_imports(mock_client, mock_environment, mock_workspace):
    debugger = QuickDebugger(
        client=mock_client,
        environment=mock_environment,
        workspace=mock_workspace,
    )

    related = debugger._find_related_files(
        error_output="ImportError in test_list_expenses.py",
        code="from add_expense import ExpenseTracker\n",
        test_code="from add_expense import ExpenseTracker\n",
        code_filename="list_expenses.py",
        test_filename="test_list_expenses.py",
        workspace_files=["add_expense.py", "list_expenses.py", "test_list_expenses.py"],
    )

    assert "add_expense.py" in related
    # Should not include the code or test file themselves
    assert "list_expenses.py" not in related
    assert "test_list_expenses.py" not in related

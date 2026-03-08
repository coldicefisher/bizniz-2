import json
from bizniz.utils.code_metadata import build_metadata_block
from bizniz.autotester.types import AutotesterResult


CODE_WITH_METADATA = (
    build_metadata_block({"problem_statement": "Add two integers and return the sum."})
    + "\n\ndef add(a, b):\n    return a + b\n"
)

CODE_WITHOUT_METADATA = "def add(a, b):\n    return a + b\n"


def test_from_code_returns_result(autotester, mock_workspace):
    mock_workspace.read_file.return_value = CODE_WITH_METADATA

    result = autotester.process_from_code(
        code_path="add.py",
        output_path="test_add.py",
    )

    assert isinstance(result, AutotesterResult)
    assert result.success is True
    assert result.mode == "from_code"


def test_from_code_reads_code_from_workspace(autotester, mock_workspace):
    mock_workspace.read_file.return_value = CODE_WITH_METADATA
    autotester.process_from_code(code_path="add.py", output_path="test_add.py")
    mock_workspace.read_file.assert_called_with(path="add.py")


def test_from_code_extracts_problem_statement_for_ai(autotester, mock_client, mock_workspace):
    mock_workspace.read_file.return_value = CODE_WITH_METADATA
    autotester.process_from_code(code_path="add.py", output_path="test_add.py")

    _, kwargs = mock_client.get_text.call_args
    messages = kwargs.get("messages") or mock_client.get_text.call_args[0][0]
    user_messages = [m for m in messages if m.get("role") == "user"]
    combined = " ".join(m.get("content", "") for m in user_messages)
    assert "Add two integers" in combined


def test_from_code_falls_back_when_no_metadata(autotester, mock_client, mock_workspace):
    mock_workspace.read_file.return_value = CODE_WITHOUT_METADATA
    # Should not raise — falls back to "(no problem statement found)"
    result = autotester.process_from_code(code_path="add.py", output_path="test_add.py")
    assert result.success is True


def test_from_code_saves_tests_to_workspace(autotester, mock_workspace):
    mock_workspace.read_file.return_value = CODE_WITH_METADATA
    autotester.process_from_code(code_path="add.py", output_path="test_add.py")
    mock_workspace.write_file.assert_called_once_with(
        path="test_add.py",
        content=mock_workspace.write_file.call_args.kwargs.get("content")
        or mock_workspace.write_file.call_args[0][1],
    )

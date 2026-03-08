from bizniz.utils.code_metadata import build_metadata_block
from bizniz.autotester.types import AutotesterResult


CODE = (
    build_metadata_block({"problem_statement": "Parse a CSV row into a list of strings."})
    + "\n\ndef parse_csv_row(row):\n    return row.split(',')\n"
)

EXISTING_TESTS = "def test_basic():\n    assert parse_csv_row('a,b') == ['a', 'b']\n"


def test_review_returns_result(autotester, mock_workspace):
    mock_workspace.read_file.side_effect = [CODE, EXISTING_TESTS]

    result = autotester.review_tests(
        code_path="csv_parser.py",
        test_path="test_csv_parser.py",
        output_path="test_csv_parser.py",
    )

    assert isinstance(result, AutotesterResult)
    assert result.success is True
    assert result.mode == "review"


def test_review_reads_both_code_and_tests(autotester, mock_workspace):
    mock_workspace.read_file.side_effect = [CODE, EXISTING_TESTS]

    autotester.review_tests(
        code_path="csv_parser.py",
        test_path="test_csv_parser.py",
        output_path="test_csv_parser.py",
    )

    calls = [c.kwargs.get("path") or c.args[0] for c in mock_workspace.read_file.call_args_list]
    assert "csv_parser.py" in calls
    assert "test_csv_parser.py" in calls


def test_review_passes_existing_tests_to_ai(autotester, mock_client, mock_workspace):
    mock_workspace.read_file.side_effect = [CODE, EXISTING_TESTS]

    autotester.review_tests(
        code_path="csv_parser.py",
        test_path="test_csv_parser.py",
        output_path="test_csv_parser.py",
    )

    _, kwargs = mock_client.get_text.call_args
    messages = kwargs.get("messages") or mock_client.get_text.call_args[0][0]
    user_messages = [m for m in messages if m.get("role") == "user"]
    combined = " ".join(m.get("content", "") for m in user_messages)
    assert "test_basic" in combined or "parse_csv_row" in combined


def test_review_saves_improved_tests(autotester, mock_workspace):
    mock_workspace.read_file.side_effect = [CODE, EXISTING_TESTS]

    autotester.review_tests(
        code_path="csv_parser.py",
        test_path="test_csv_parser.py",
        output_path="test_csv_parser_v2.py",
    )

    write_call = mock_workspace.write_file.call_args
    path_arg = write_call.kwargs.get("path") or write_call.args[0]
    assert path_arg == "test_csv_parser_v2.py"

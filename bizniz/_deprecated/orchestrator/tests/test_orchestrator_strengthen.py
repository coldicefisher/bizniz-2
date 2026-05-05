def test_strengthen_tests_delegates_to_autotester(orchestrator, mock_autotester):
    orchestrator.strengthen_tests(
        code_filename="add.py",
        test_filename="test_add.py",
    )

    mock_autotester.review_tests.assert_called_once_with(
        code_path="add.py",
        test_path="test_add.py",
        output_path="test_add.py",
    )


def test_strengthen_tests_custom_output(orchestrator, mock_autotester):
    orchestrator.strengthen_tests(
        code_filename="add.py",
        test_filename="test_add.py",
        output_filename="test_add_v2.py",
    )

    mock_autotester.review_tests.assert_called_once_with(
        code_path="add.py",
        test_path="test_add.py",
        output_path="test_add_v2.py",
    )

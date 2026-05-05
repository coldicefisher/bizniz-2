from bizniz.orchestrator.coding_orchestrator import _detect_missing_package


def test_detect_module_not_found():
    error = "ModuleNotFoundError: No module named 'requests'"
    assert _detect_missing_package(error) == "requests"


def test_detect_import_error():
    error = "ImportError: No module named 'numpy'"
    assert _detect_missing_package(error) == "numpy"


def test_no_missing_package():
    error = "AssertionError: 1 != 2"
    assert _detect_missing_package(error) is None


def test_detect_in_longer_output():
    error = (
        "Traceback (most recent call last):\n"
        "  File 'test.py', line 1, in <module>\n"
        "    import pandas\n"
        "ModuleNotFoundError: No module named 'pandas'\n"
    )
    assert _detect_missing_package(error) == "pandas"

import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "functional" not in [m.name for m in item.iter_markers()]:
            item.add_marker(pytest.mark.functional)

from pathlib import Path

import pytest

_FUNCTIONAL_DIR = Path(__file__).parent.resolve()


def pytest_collection_modifyitems(config, items):
    """Mark every test in THIS directory tree as functional.

    Scoped to the directory of this conftest — without the path check,
    pytest applies the hook to every collected item in the session,
    which silently marks unrelated tests in other packages as
    functional and excludes them from default `-m 'not functional'` runs.
    """
    for item in items:
        try:
            item_path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        if _FUNCTIONAL_DIR not in item_path.parents and item_path != _FUNCTIONAL_DIR:
            continue
        if "functional" not in [m.name for m in item.iter_markers()]:
            item.add_marker(pytest.mark.functional)

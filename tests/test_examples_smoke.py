"""Smoke-import every script under ``examples/`` to catch bit-rot.

The pre-v2.5 examples directory had 22 scripts; 10 of them silently
broke when the v2.5 refactor (commit ``e0fc7e7``, 2026-05-06) moved
``bizniz.agents.coder`` → ``bizniz.coder`` and removed
``bizniz.tester``. Two of those — ``auto_architect.py`` and
``milestone_build.py`` — were the documented entry points in
CLAUDE.md, and broke for weeks before someone reached for one and
discovered.

This test imports each ``examples/*.py`` and asserts it loads. It
does NOT run them (they kick off real LLM calls / docker builds).
Just that the module loads — which catches:

  - Moved-but-not-updated imports
  - Deleted symbols still referenced
  - Renamed packages

``examples/_deprecated/`` is excluded; those scripts are known-stale
and kept only for git archaeology.

Run with: ``.venv/bin/python -m pytest tests/test_examples_smoke.py -q``
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import List

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"


def _current_example_scripts() -> List[Path]:
    """Every ``examples/*.py`` NOT in ``_deprecated/``."""
    if not EXAMPLES_DIR.is_dir():
        return []
    return sorted(
        p for p in EXAMPLES_DIR.glob("*.py")
        if p.is_file() and not p.name.startswith("_")
    )


@pytest.mark.parametrize(
    "script_path",
    _current_example_scripts(),
    ids=lambda p: p.stem,
)
def test_example_imports_cleanly(script_path):
    """Loading the module must not raise. Scripts that call
    ``sys.exit()`` at import time are allowed (some use it for
    arg-parsing guards)."""
    spec = importlib.util.spec_from_file_location(
        f"examples_smoke.{script_path.stem}", script_path,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        # Some scripts call sys.exit() early when run without args.
        # The fact that they got past import to the SystemExit is
        # what we care about — imports resolved.
        pass
    except ModuleNotFoundError as e:
        pytest.fail(
            f"{script_path.name} imports a module that no longer "
            f"exists: {e}. If this script is intentionally stale, "
            f"move it to examples/_deprecated/."
        )

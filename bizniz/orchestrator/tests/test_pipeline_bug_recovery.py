"""End-to-end recovery tests for the two pipeline bugs surfaced 2026-04-29.

Both bugs were already addressed in code (`_is_source_import_error` for Bug
1; `_CONFIG_FILENAMES` allowlist + writable-scope expansion for Bug 2), and
their helper functions have unit tests in
``test_collection_error_classification.py``. What was missing — and what
this file provides — is **deliberate orchestrator-loop tests** that drive
``run_multi`` end-to-end and assert the right recovery branch fires:

  Bug 1 — collection-error misclassification (source vs test).
    A pytest exit-code-2 with a NameError in a workspace source file must
    route to ``coder.repair_multi`` (source repair). Without the fix it
    routed to ``tester.generate_multi`` (test regeneration), wasting
    iterations on the wrong layer.

  Bug 2 — read-only filter blocks valid config fixes.
    When the failure mode is "config file misconfigured" (e.g. jest.config
    pointing at a missing dir, package.json missing a dep), the AI's repair
    targets the config file. The orchestrator must let that change through
    even though the config isn't in the issue's ``target_files``. Without
    the fix the change was filtered out as "read-only" and the issue stalled.

Both tests use the existing mocked-orchestrator pattern from
``test_orchestrator_multi.py``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.agents.coder.coder import Coder
from bizniz.agents.coder.types import CoderProcessResult, FileChange
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import (
    ExecutionEnvironmentErrorDetails,
    ExecutionEnvironmentResult,
)
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
from bizniz.tester.tester import Tester
from bizniz.tester.types import GeneratedTestFile, TesterResult
from bizniz.workspace.base_workspace import BaseWorkspace


# ── Shared fixture infrastructure ────────────────────────────────────────────


CODE_APP = "from fastapi import FastAPI\napp = FastAPI()\n"
TESTS_APP = (
    "from pet_groomer.app import app\n"
    "def test_app_exists():\n    assert app is not None\n"
)


def _coder_initial_generate(**kwargs):
    """Initial code-gen produces the source files the test plans for."""
    target_files = kwargs.get("target_files", [])
    return CoderProcessResult(changes=[
        FileChange(filepath=tf["filepath"], code=CODE_APP, action="create")
        for tf in target_files
    ])


def _tester_initial_generate(**kwargs):
    test_files = kwargs.get("test_files", [])
    return TesterResult(
        test_files=[GeneratedTestFile(filepath=fp, tests=TESTS_APP)
                    for fp in test_files],
        mode="from_prompt",
        success=True,
    )


@pytest.fixture
def mock_test_env():
    return MagicMock(spec=BaseExecutionEnvironment)


@pytest.fixture
def mock_workspace(tmp_path):
    """Workspace with ``list_relative_files`` returning per-test contents.

    Tests override ``list_relative_files.return_value`` to put extra
    files (e.g. jest.config.js) on the simulated disk.
    """
    ws = MagicMock(spec=BaseWorkspace)
    ws.path.side_effect = lambda p: tmp_path / p
    ws.exists.return_value = False
    ws.read_file.return_value = ""
    ws.list_relative_files.return_value = []
    return ws


def _make_orchestrator(coder, tester, env, workspace, **kwargs):
    return CodingOrchestrator(
        coder=coder, tester=tester, test_environment=env,
        workspace=workspace, max_iterations=8, **kwargs,
    )


# ── Bug 1: collection-error source repair ──────────────────────────────────


def test_bug1_source_collection_error_routes_to_source_repair(
    mock_test_env, mock_workspace,
):
    """When pytest exit-code-2 traceback points at a source file (NameError
    inside our code, surfacing as a test-collection failure), the orchestrator
    must call ``coder.repair_multi`` — not ``tester.generate_multi`` for
    regeneration."""
    coder = MagicMock(spec=Coder)
    coder.generate_multi.side_effect = _coder_initial_generate
    coder.repair_multi.return_value = CoderProcessResult(
        changes=[FileChange(
            filepath="pet_groomer/app.py",
            code=CODE_APP + "# fixed: imports were complete\n",
            action="modify",
        )],
        dependencies=[],
    )
    coder.repair_multi_inline.return_value = CoderProcessResult(
        changes=[], dependencies=[],
    )

    tester = MagicMock(spec=Tester)
    tester.generate_multi.side_effect = _tester_initial_generate
    tester.process_from_prompt.return_value = TesterResult(
        test_files=[GeneratedTestFile(filepath="tests/test_app.py", tests=TESTS_APP)],
        mode="from_prompt", success=True,
    )

    # First execute: collection error with traceback pointing at our source.
    # Second execute: success.
    source_collection_error = ExecutionEnvironmentResult(
        success=False,
        error=ExecutionEnvironmentErrorDetails(
            type="TestFailure",
            message="pytest exited with code 2",
            traceback=(
                "____________ ERROR collecting tests/test_app.py ____________\n"
                "tests/test_app.py:1: in <module>\n"
                "    from pet_groomer.app import app\n"
                "pet_groomer/app.py:7: NameError: name 'FastAPI' is not defined\n"
            ),
        ),
        stdout=(
            "tests/test_app.py:1: in <module>\n"
            "    from pet_groomer.app import app\n"
            "pet_groomer/app.py:7: NameError: name 'FastAPI' is not defined\n"
        ),
    )
    mock_test_env.execute.side_effect = [
        source_collection_error,
        ExecutionEnvironmentResult(success=True),
    ]

    orchestrator = _make_orchestrator(coder, tester, mock_test_env, mock_workspace)

    # Capture the regen-tests call count BEFORE running so we can isolate
    # any post-failure regenerations from the initial scaffold gen.
    initial_tester_calls = tester.generate_multi.call_count
    initial_regen_calls = tester.process_from_prompt.call_count

    result = orchestrator.run_multi(
        prompt="Build pet groomer FastAPI app",
        target_files=[{"filepath": "pet_groomer/app.py", "action": "create"}],
        test_files=["tests/test_app.py"],
    )

    # 1. Source repair was triggered.
    assert coder.repair_multi.call_count >= 1, (
        "coder.repair_multi should be called for source-side collection error; "
        "if it wasn't, the orchestrator misclassified the error and went down "
        "the regenerate-tests path (Bug 1)."
    )

    # 2. The error message handed to repair signals it's a source problem.
    repair_kwargs = coder.repair_multi.call_args.kwargs
    repair_msg = repair_kwargs.get("error_message", "")
    assert "SOURCE CODE" in repair_msg.upper() or "source code" in repair_msg, (
        f"Source-repair prompt should mention SOURCE CODE; got: {repair_msg[:200]!r}"
    )

    # 3. No test regeneration happened — that's the buggy branch.
    final_regen_calls = tester.process_from_prompt.call_count
    assert final_regen_calls == initial_regen_calls, (
        f"Test regeneration happened ({final_regen_calls - initial_regen_calls} "
        f"new calls) when source repair was the right move — Bug 1 regression."
    )

    # 4. Pipeline recovered (second execute returned success).
    assert result.success is True


def test_bug1_test_collection_error_still_regenerates_tests(
    mock_test_env, mock_workspace,
):
    """The classifier's negative path: a collection error caused by a
    test-file problem (undefined fixture) must still go down the
    regenerate-tests branch. Guards against an over-zealous fix.
    """
    coder = MagicMock(spec=Coder)
    coder.generate_multi.side_effect = _coder_initial_generate
    coder.repair_multi.return_value = CoderProcessResult(changes=[], dependencies=[])

    tester = MagicMock(spec=Tester)
    tester.generate_multi.side_effect = _tester_initial_generate
    tester.process_from_prompt.return_value = TesterResult(
        test_files=[GeneratedTestFile(filepath="tests/test_app.py", tests=TESTS_APP)],
        mode="from_prompt", success=True,
    )

    test_collection_error = ExecutionEnvironmentResult(
        success=False,
        error=ExecutionEnvironmentErrorDetails(
            type="TestFailure",
            message="pytest exited with code 2",
            traceback=(
                "____________ ERROR collecting tests/test_app.py ____________\n"
                "file /workspace/tests/test_app.py, line 12\n"
                "  def test_something(undefined_fixture):\n"
                "E       fixture 'undefined_fixture' not found\n"
            ),
        ),
        stdout="E   fixture 'undefined_fixture' not found",
    )
    mock_test_env.execute.side_effect = [
        test_collection_error,
        ExecutionEnvironmentResult(success=True),
    ]

    orchestrator = _make_orchestrator(coder, tester, mock_test_env, mock_workspace)

    # ``run_multi`` regenerates via ``tester.generate_multi`` (not
    # ``process_from_prompt``). Capture the initial-scaffold count so we
    # can detect a post-failure regeneration above the baseline.
    initial_generate_calls = tester.generate_multi.call_count

    result = orchestrator.run_multi(
        prompt="Build pet groomer",
        target_files=[{"filepath": "pet_groomer/app.py", "action": "create"}],
        test_files=["tests/test_app.py"],
    )

    # Test regeneration happened — that's the correct branch here.
    assert tester.generate_multi.call_count > initial_generate_calls, (
        "Test-side collection error should regenerate tests; the classifier "
        "may be misfiring as 'source' when it shouldn't."
    )
    # Source repair did NOT fire.
    assert coder.repair_multi.call_count == 0, (
        "coder.repair_multi must NOT fire for a test-side collection error."
    )
    assert result.success is True


# ── Bug 2: config-file repair survives the read-only filter ─────────────────


def test_bug2_config_file_repair_survives_readonly_filter(
    tmp_path, mock_test_env, mock_workspace,
):
    """The failing test points at jest.config — the AI patches that file
    even though it's not in target_files. The orchestrator must NOT
    filter the change out as 'read-only', because jest.config.js is on
    the universal config allowlist.
    """
    # Stand up a real jest.config.js on the workspace's tmp_path so
    # ``list_relative_files`` and ``read_text`` find it.
    workspace_root = tmp_path
    jest_path = workspace_root / "jest.config.js"
    jest_path.write_text("module.exports = { roots: ['tests'] };\n")

    mock_workspace.list_relative_files.return_value = [
        Path("pkg/foo.py"),
        Path("tests/test_foo.py"),
        Path("jest.config.js"),
    ]

    # Make the workspace's ``path()`` return a real Path so the
    # orchestrator can ``read_text`` it.
    mock_workspace.path.side_effect = lambda p: workspace_root / p

    coder = MagicMock(spec=Coder)
    coder.generate_multi.side_effect = _coder_initial_generate
    # Regular failure repair — AI patches jest.config.js.
    coder.repair_multi_inline.return_value = CoderProcessResult(
        changes=[
            FileChange(
                filepath="jest.config.js",
                code="module.exports = { roots: ['<rootDir>/tests'], testEnvironment: 'jsdom' };\n",
                action="modify",
            ),
        ],
        dependencies=[],
    )
    coder.repair_multi.return_value = CoderProcessResult(changes=[], dependencies=[])

    tester = MagicMock(spec=Tester)
    tester.generate_multi.side_effect = _tester_initial_generate

    # First execute: regular failure (exit code 1) — config issue manifesting
    # as failed tests, not a collection error. This is the path through
    # _inline_repair where the read-only filter applies.
    mock_test_env.execute.side_effect = [
        ExecutionEnvironmentResult(
            success=False,
            error=ExecutionEnvironmentErrorDetails(
                type="TestFailure",
                message="pytest exited with code 1",
                traceback=(
                    "FAILED tests/test_foo.py::test_something\n"
                    "Validation Error: jest config 'roots' references a path that doesn't exist\n"
                ),
            ),
            stdout="FAILED tests/test_foo.py::test_something",
        ),
        ExecutionEnvironmentResult(success=True),
    ]

    orchestrator = _make_orchestrator(coder, tester, mock_test_env, mock_workspace)

    written: dict[str, str] = {}

    def _capture_write(path, content, **_):
        written[path] = content

    mock_workspace.write_file.side_effect = _capture_write

    result = orchestrator.run_multi(
        prompt="Build pkg/foo.py",
        target_files=[{"filepath": "pkg/foo.py", "action": "create"}],
        test_files=["tests/test_foo.py"],
    )

    # 1. Inline repair fired.
    assert coder.repair_multi_inline.call_count >= 1, (
        "coder.repair_multi_inline must fire for a regular test failure."
    )

    # 2. The jest.config.js was offered as a writable file, not just
    # readonly context. Inspect the call that the orchestrator made.
    call_kwargs = coder.repair_multi_inline.call_args.kwargs
    source_files = call_kwargs.get("source_files", {})
    readonly_files = call_kwargs.get("readonly_context", {})
    assert "jest.config.js" in source_files, (
        f"jest.config.js must be in writable source_files via the config "
        f"allowlist (Bug 2). Got source_files={list(source_files)}, "
        f"readonly={list(readonly_files)}"
    )

    # 3. The repair to jest.config.js was actually written to the workspace
    # (NOT filtered out as 'read-only'). This is the behavioral guarantee.
    assert "jest.config.js" in written, (
        f"jest.config.js change was filtered out by the read-only filter — "
        f"Bug 2 regression. Written files: {list(written)}"
    )
    assert "<rootDir>" in written["jest.config.js"], (
        "Repair content was not preserved through the write."
    )

    # 4. Pipeline recovered.
    assert result.success is True


def test_bug2_non_config_readonly_files_still_filtered(
    tmp_path, mock_test_env, mock_workspace,
):
    """Negative companion to Bug 2: non-config files outside target_files
    must still be filtered. The fix gives config files a special pass; it
    must not become a free-for-all on every file in the workspace.
    """
    workspace_root = tmp_path
    other_path = workspace_root / "pkg" / "other.py"
    other_path.parent.mkdir(parents=True)
    other_path.write_text("# from a prior issue, read-only here\n")

    mock_workspace.list_relative_files.return_value = [
        Path("pkg/foo.py"),
        Path("pkg/other.py"),
        Path("tests/test_foo.py"),
    ]
    mock_workspace.path.side_effect = lambda p: workspace_root / p

    coder = MagicMock(spec=Coder)
    coder.generate_multi.side_effect = _coder_initial_generate
    coder.repair_multi_inline.return_value = CoderProcessResult(
        changes=[
            FileChange(
                filepath="pkg/other.py",  # NOT a config file
                code="# AI tried to edit a prior issue's file\n",
                action="modify",
            ),
        ],
        dependencies=[],
    )
    coder.repair_multi.return_value = CoderProcessResult(changes=[], dependencies=[])

    tester = MagicMock(spec=Tester)
    tester.generate_multi.side_effect = _tester_initial_generate

    mock_test_env.execute.side_effect = [
        ExecutionEnvironmentResult(
            success=False,
            error=ExecutionEnvironmentErrorDetails(
                type="TestFailure", message="pytest exited with code 1",
                traceback="FAILED tests/test_foo.py",
            ),
            stdout="FAILED",
        ),
        ExecutionEnvironmentResult(success=True),
    ]

    orchestrator = _make_orchestrator(coder, tester, mock_test_env, mock_workspace)

    written: dict[str, str] = {}
    mock_workspace.write_file.side_effect = (
        lambda path, content, **_: written.setdefault(path, content)
    )

    orchestrator.run_multi(
        prompt="Build pkg/foo.py",
        target_files=[{"filepath": "pkg/foo.py", "action": "create"}],
        test_files=["tests/test_foo.py"],
    )

    # The non-config edit was filtered.
    assert "pkg/other.py" not in written, (
        "Non-config read-only files must still be filtered. The Bug 2 fix "
        "shouldn't make every file writable."
    )

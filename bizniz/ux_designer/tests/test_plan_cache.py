"""Tests for plan_cache (item #4)."""
import json
import time
from pathlib import Path
from textwrap import dedent

import pytest

from bizniz.ux_designer.plan_cache import (
    compute_input_mtime,
    is_cache_valid,
    load_cache,
    save_cache,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip())


class TestComputeInputMtime:
    def test_returns_none_when_empty(self, tmp_path):
        assert compute_input_mtime(tmp_path) is None

    def test_picks_newest_across_globs(self, tmp_path):
        _write(tmp_path / "src/App.tsx", "export default () => null;")
        time.sleep(0.01)
        _write(tmp_path / "src/index.css", "/* tw */")
        m = compute_input_mtime(tmp_path)
        assert m == (tmp_path / "src/index.css").stat().st_mtime

    def test_includes_named_files(self, tmp_path):
        _write(tmp_path / "tailwind.config.ts", "export default {};")
        m = compute_input_mtime(tmp_path)
        assert m == (tmp_path / "tailwind.config.ts").stat().st_mtime

    def test_package_json_picked_up(self, tmp_path):
        _write(tmp_path / "package.json", '{"name": "x"}')
        time.sleep(0.01)
        _write(tmp_path / "src/App.tsx", "export default () => null;")
        m = compute_input_mtime(tmp_path)
        # App.tsx was newer than package.json.
        assert m == (tmp_path / "src/App.tsx").stat().st_mtime


class TestSaveLoad:
    def test_round_trip(self, tmp_path):
        plan = {"app_type": "hybrid", "design_system": {"palette": {}}}
        gfr = {"status": "passed", "files_written": ["a.ts"]}
        # Make the referenced file exist so the helper can stat it.
        (tmp_path / "a.ts").write_text("// content")
        save_cache(
            tmp_path,
            plan=plan,
            global_fix_result=gfr,
            input_mtime=1700000000.0,
        )
        cached = load_cache(tmp_path)
        assert cached is not None
        assert cached["plan"] == plan
        assert cached["global_fix_result"] == gfr
        assert cached["input_mtime"] == 1700000000.0
        assert "a.ts" in cached["files_written_mtimes"]

    def test_load_missing_returns_none(self, tmp_path):
        assert load_cache(tmp_path) is None

    def test_load_corrupt_returns_none(self, tmp_path):
        fp = tmp_path / ".bizniz" / "ux_plan.json"
        fp.parent.mkdir(parents=True)
        fp.write_text("not json {")
        assert load_cache(tmp_path) is None


class TestIsCacheValid:
    def _seed(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src/App.tsx").write_text("// app")
        time.sleep(0.01)
        save_cache(
            tmp_path,
            plan={"app_type": "hybrid"},
            global_fix_result={"files_written": []},
            input_mtime=compute_input_mtime(tmp_path),
        )
        return load_cache(tmp_path)

    def test_valid_when_inputs_unchanged(self, tmp_path):
        cached = self._seed(tmp_path)
        valid, _ = is_cache_valid(
            cached,
            current_input_mtime=cached["input_mtime"],
            workspace_root=tmp_path,
        )
        assert valid is True

    def test_invalid_when_input_newer(self, tmp_path):
        cached = self._seed(tmp_path)
        # Newer mtime than recorded.
        newer = (cached["input_mtime"] or 0) + 10.0
        valid, reason = is_cache_valid(
            cached,
            current_input_mtime=newer,
            workspace_root=tmp_path,
        )
        assert valid is False
        assert "input files changed" in reason

    def test_invalid_when_output_removed(self, tmp_path):
        # Cache includes a global-design output file. Delete it.
        (tmp_path / "src").mkdir()
        (tmp_path / "src/App.tsx").write_text("// app")
        (tmp_path / "primitives.tsx").write_text("// p")
        save_cache(
            tmp_path,
            plan={"app_type": "hybrid"},
            global_fix_result={"files_written": ["primitives.tsx"]},
            input_mtime=compute_input_mtime(tmp_path),
        )
        cached = load_cache(tmp_path)
        # Remove the previously-written output.
        (tmp_path / "primitives.tsx").unlink()
        valid, reason = is_cache_valid(
            cached,
            current_input_mtime=cached["input_mtime"],
            workspace_root=tmp_path,
        )
        assert valid is False
        assert "removed" in reason

    def test_invalid_when_no_inputs_now(self, tmp_path):
        cached = self._seed(tmp_path)
        valid, reason = is_cache_valid(
            cached,
            current_input_mtime=None,
            workspace_root=tmp_path,
        )
        assert valid is False
        assert "no input files" in reason

    def test_invalid_when_cache_missing_mtime(self, tmp_path):
        valid, reason = is_cache_valid(
            {},
            current_input_mtime=1700000000.0,
            workspace_root=tmp_path,
        )
        assert valid is False
        assert "missing input_mtime" in reason

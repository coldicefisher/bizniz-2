"""Tests for the AST/regex anti-pattern scanner."""
from __future__ import annotations

import textwrap

import pytest

from bizniz.refactorer.anti_patterns import (
    AntiPatternFinding,
    AntiPatternReport,
    _is_test_path,
    scan_files,
    scan_python_file,
    scan_typescript_file,
)


def _reader(files: dict):
    return lambda p: files[p]


# ── _is_test_path ────────────────────────────────────────────────


class TestIsTestPath:
    @pytest.mark.parametrize("p", [
        "/x/tests/foo.py",
        "/x/test/foo.py",
        "/x/conftest.py",
        "/x/tests/conftest.py",
        "/x/foo/test_thing.py",
        "/x/foo/conftest/helper.py",
    ])
    def test_classifies_test(self, p):
        assert _is_test_path(p) is True

    @pytest.mark.parametrize("p", [
        "/x/app/main.py",
        "/x/lib/util.py",
        "/x/src/index.ts",
        "/x/testing_unrelated.py",  # name doesn't start with test_
    ])
    def test_classifies_non_test(self, p):
        assert _is_test_path(p) is False


# ── Python AST scanner ───────────────────────────────────────────


class TestPythonDropAll:
    def test_drop_all_in_test_fixture_is_critical(self):
        src = textwrap.dedent("""
            from app.db.base import Base
            from app.db.session import engine

            async def teardown():
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.drop_all)
        """).strip()
        findings = scan_python_file(
            "/x/tests/conftest.py", text=src,
        )
        drop = [f for f in findings if f.pattern == "drop_all_in_test"]
        assert len(drop) == 1
        assert drop[0].severity == "critical"
        assert "transactional rollback" in drop[0].suggested_fix.lower()

    def test_drop_all_in_non_test_is_warning(self):
        src = "Base.metadata.drop_all(engine)"
        findings = scan_python_file(
            "/x/app/admin.py", text=src,
        )
        drop = [f for f in findings if f.pattern == "drop_all_call"]
        assert len(drop) == 1
        assert drop[0].severity == "warning"


class TestPythonExceptHandlers:
    def test_bare_except_critical(self):
        src = textwrap.dedent("""
            try:
                do_thing()
            except:
                pass
        """).strip()
        findings = scan_python_file("/x/app.py", text=src)
        bare = [f for f in findings if f.pattern == "bare_except"]
        assert len(bare) == 1
        assert bare[0].severity == "critical"

    def test_swallow_exception_warning(self):
        src = textwrap.dedent("""
            try:
                do_thing()
            except Exception:
                pass
        """).strip()
        findings = scan_python_file("/x/app.py", text=src)
        swallow = [f for f in findings if f.pattern == "swallow_exception"]
        assert len(swallow) == 1
        assert swallow[0].severity == "warning"

    def test_except_with_log_or_reraise_ok(self):
        src = textwrap.dedent("""
            try:
                do_thing()
            except Exception as e:
                log.exception(e)
                raise
        """).strip()
        findings = scan_python_file("/x/app.py", text=src)
        assert not any(f.pattern.startswith("swallow") for f in findings)
        assert not any(f.pattern == "bare_except" for f in findings)


class TestPythonEnvironClear:
    def test_environ_clear_in_test_critical(self):
        src = "import os\nos.environ.clear()"
        findings = scan_python_file(
            "/x/tests/test_env.py", text=src,
        )
        env = [f for f in findings if f.pattern == "environ_clear_in_test"]
        assert len(env) == 1
        assert env[0].severity == "critical"


class TestPythonSubprocessShell:
    def test_shell_true_flagged(self):
        src = textwrap.dedent("""
            import subprocess
            subprocess.run("ls -la", shell=True)
        """).strip()
        findings = scan_python_file("/x/app.py", text=src)
        sub = [f for f in findings if f.pattern == "subprocess_shell_true"]
        assert len(sub) == 1

    def test_shell_false_not_flagged(self):
        src = textwrap.dedent("""
            import subprocess
            subprocess.run(["ls", "-la"], shell=False)
        """).strip()
        findings = scan_python_file("/x/app.py", text=src)
        assert not any(
            f.pattern == "subprocess_shell_true" for f in findings
        )


class TestPythonEval:
    def test_eval_call_flagged(self):
        src = "result = eval('1 + 2')"
        findings = scan_python_file("/x/app.py", text=src)
        ev = [f for f in findings if f.pattern == "eval_call"]
        assert len(ev) == 1


class TestHardcodedCredentials:
    def test_password_literal_flagged(self):
        src = 'password = "hunter2real"'
        findings = scan_python_file("/x/app.py", text=src)
        cred = [f for f in findings if f.pattern == "hardcoded_credential"]
        assert len(cred) == 1
        assert cred[0].severity == "critical"

    def test_env_var_lookup_not_flagged(self):
        src = 'password = os.environ["DB_PASSWORD"]'
        findings = scan_python_file("/x/app.py", text=src)
        assert not any(f.pattern == "hardcoded_credential" for f in findings)

    def test_settings_lookup_not_flagged(self):
        src = 'api_key = settings.openai_api_key'
        findings = scan_python_file("/x/app.py", text=src)
        assert not any(f.pattern == "hardcoded_credential" for f in findings)

    def test_placeholder_strings_not_flagged(self):
        for placeholder in ("password", "changeme", "todo", "example",
                            "fill-in", "your-key-here"):
            src = f'api_key = "{placeholder}"'
            findings = scan_python_file("/x/app.py", text=src)
            assert not any(
                f.pattern == "hardcoded_credential" for f in findings
            ), f"placeholder {placeholder!r} got flagged"


# ── TypeScript/JavaScript scanner ────────────────────────────────


class TestTSConsoleLog:
    def test_console_log_in_source_flagged(self):
        src = 'function f() { console.log("hi"); }'
        findings = scan_typescript_file("/x/src/util.ts", text=src)
        cl = [f for f in findings if f.pattern == "console_log_in_source"]
        assert len(cl) == 1
        assert cl[0].severity == "info"

    def test_console_log_in_test_ignored(self):
        src = 'console.log("debugging");'
        findings = scan_typescript_file(
            "/x/tests/util.test.ts", text=src,
        )
        assert not any(f.pattern == "console_log_in_source" for f in findings)


class TestTSEval:
    def test_eval_flagged(self):
        src = 'const x = eval("1 + 2");'
        findings = scan_typescript_file("/x/src/x.ts", text=src)
        ev = [f for f in findings if f.pattern == "eval_call"]
        assert len(ev) == 1


# ── scan_files dispatcher ────────────────────────────────────────


class TestScanFiles:
    def test_dispatches_by_extension(self):
        files = {
            "/x/a.py": "x = 1",
            "/x/b.ts": "const y = 1;",
            "/x/c.md": "# unrelated",
        }
        report = scan_files(files.keys(), file_reader=_reader(files))
        assert report.files_scanned == 2
        assert "/x/c.md" in report.files_skipped

    def test_aggregates_across_files(self):
        files = {
            "/x/tests/a.py": (
                "from app.db.base import Base\n"
                "Base.metadata.drop_all(engine)"
            ),
            "/x/b.py": "try:\n    f()\nexcept:\n    pass",
        }
        report = scan_files(files.keys(), file_reader=_reader(files))
        # One drop_all + one bare_except + 0 from b's drop_all.
        patterns = {f.pattern for f in report.findings}
        assert "drop_all_in_test" in patterns
        assert "bare_except" in patterns

    def test_by_severity_helper(self):
        files = {
            "/x/a.py": 'password = "real_secret_here"',
        }
        report = scan_files(files.keys(), file_reader=_reader(files))
        assert len(report.by_severity("critical")) == 1
        assert len(report.by_severity("warning")) == 0

    def test_by_pattern_helper(self):
        files = {
            "/x/a.py": "try:\n    f()\nexcept:\n    pass\ntry:\n    g()\nexcept:\n    pass",
        }
        report = scan_files(files.keys(), file_reader=_reader(files))
        bare = report.by_pattern("bare_except")
        assert len(bare) == 2

    def test_syntax_error_silently_skipped(self):
        files = {"/x/broken.py": "def f("}  # incomplete
        report = scan_files(files.keys(), file_reader=_reader(files))
        # Syntax errors yield zero findings but the file still counts
        # as scanned.
        assert report.files_scanned == 1
        assert not any(f.path == "/x/broken.py" for f in report.findings)

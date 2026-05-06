"""Tests for container tool factories.

We don't actually invoke docker — we mock ``subprocess.run`` and verify
the argv shape, parameter handling, and error formatting.
"""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bizniz.lib.tools.container import (
    build_container_handlers,
    make_hit_endpoint,
    make_inspect_env,
    make_run_in_container,
    make_run_python_in_container,
    make_tail_logs,
)


def _proc(stdout="", stderr="", returncode=0):
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.returncode = returncode
    return p


# ── tail_logs ──────────────────────────────────────────────────────────


class TestTailLogs:
    def test_no_compose_path(self):
        handler = make_tail_logs("", default_service="backend")
        assert "ERROR: tail_logs unavailable" in handler({})

    def test_no_service(self):
        handler = make_tail_logs("/tmp/c.yml", default_service=None)
        assert "needs a service name" in handler({})

    def test_default_service_used(self):
        handler = make_tail_logs("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="hello logs")) as m:
            out = handler({})
        argv = m.call_args[0][0]
        assert "backend" in argv
        assert "--tail" in argv and "100" in argv
        assert "hello logs" in out

    def test_action_service_overrides(self):
        handler = make_tail_logs("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="x")) as m:
            handler({"service": "auth"})
        argv = m.call_args[0][0]
        assert "auth" in argv

    def test_lines_clamped(self):
        handler = make_tail_logs("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="x")) as m:
            handler({"path": "9999"})
        argv = m.call_args[0][0]
        assert "500" in argv  # clamp ceiling

    def test_lines_invalid_falls_back_100(self):
        handler = make_tail_logs("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="x")) as m:
            handler({"path": "not-a-number"})
        argv = m.call_args[0][0]
        assert "100" in argv

    def test_empty_logs(self):
        handler = make_tail_logs("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="")):
            out = handler({})
        assert "no logs" in out

    def test_timeout(self):
        handler = make_tail_logs("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            out = handler({})
        assert "ERROR: tail_logs timed out" in out


# ── run_in_container ───────────────────────────────────────────────────


class TestRunInContainer:
    def test_no_command(self):
        handler = make_run_in_container("/tmp/c.yml", default_service="backend")
        assert "non-empty" in handler({"command": ""})

    def test_command_passed_via_sh(self):
        handler = make_run_in_container("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="ok\n")) as m:
            out = handler({"command": "ls /workspace"})
        argv = m.call_args[0][0]
        assert argv[:6] == ["docker", "compose", "-f", "/tmp/c.yml", "exec", "-T"]
        assert "backend" in argv
        assert argv[-3:] == ["sh", "-c", "ls /workspace"]
        assert "ok" in out
        assert "exit code: 0" in out

    def test_exit_code_surfaced(self):
        handler = make_run_in_container("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="boom", returncode=2)):
            out = handler({"command": "false"})
        assert "exit code: 2" in out


# ── run_python_in_container ────────────────────────────────────────────


class TestRunPythonInContainer:
    def test_python_dash_c(self):
        handler = make_run_python_in_container("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="42\n")) as m:
            out = handler({"command": "print(42)"})
        argv = m.call_args[0][0]
        assert argv[-3:] == ["python", "-c", "print(42)"]
        assert "42" in out


# ── hit_endpoint ───────────────────────────────────────────────────────


class TestHitEndpoint:
    def test_no_url(self):
        handler = make_hit_endpoint("/tmp/c.yml", default_service="backend")
        assert "ERROR" in handler({"url": ""})

    def test_get_default(self):
        handler = make_hit_endpoint("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="HTTP/1.1 200")) as m:
            handler({"url": "http://backend:8000/health"})
        argv = m.call_args[0][0]
        assert "curl" in argv
        i = argv.index("-X")
        assert argv[i + 1] == "GET"
        assert "http://backend:8000/health" == argv[-1]

    def test_post_with_json_body(self):
        handler = make_hit_endpoint("/tmp/c.yml", default_service="backend")
        rd = json.dumps({"method": "POST", "body": {"a": 1}})
        with patch("subprocess.run", return_value=_proc(stdout="HTTP/1.1 201")) as m:
            handler({"url": "http://backend:8000/x", "request_data": rd})
        argv = m.call_args[0][0]
        i = argv.index("-X")
        assert argv[i + 1] == "POST"
        # body present
        assert "--data-binary" in argv
        bi = argv.index("--data-binary")
        assert json.loads(argv[bi + 1]) == {"a": 1}
        # default content-type added
        assert "Content-Type: application/json" in argv

    def test_explicit_headers_and_body(self):
        handler = make_hit_endpoint("/tmp/c.yml", default_service="backend")
        rd = json.dumps({
            "method": "PUT",
            "headers": {"Authorization": "Bearer xyz", "Content-Type": "text/plain"},
            "body": "raw",
        })
        with patch("subprocess.run", return_value=_proc(stdout="ok")) as m:
            handler({"url": "http://backend:8000/x", "request_data": rd})
        argv = m.call_args[0][0]
        assert "Authorization: Bearer xyz" in argv
        # When explicit Content-Type set, we don't add another
        assert argv.count("Content-Type: text/plain") == 1
        assert "Content-Type: application/json" not in argv

    def test_bad_json(self):
        handler = make_hit_endpoint("/tmp/c.yml", default_service="backend")
        out = handler({"url": "http://backend/x", "request_data": "{not json"})
        assert "could not parse request_data" in out

    def test_curl_missing(self):
        handler = make_hit_endpoint("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", side_effect=FileNotFoundError("no curl")):
            out = handler({"url": "http://backend/x"})
        assert "curl not available" in out


# ── inspect_env ────────────────────────────────────────────────────────


class TestInspectEnv:
    def test_filters_by_prefix(self):
        handler = make_inspect_env("/tmp/c.yml", default_service="backend")
        env = "PATH=/x\nFUSIONAUTH_ISSUER=https://fa\nFUSIONAUTH_API_KEY=secret\nHOME=/r\n"
        with patch("subprocess.run", return_value=_proc(stdout=env)):
            out = handler({"path": "FUSIONAUTH"})
        assert "FUSIONAUTH_ISSUER" in out
        assert "FUSIONAUTH_API_KEY" in out
        assert "PATH=" not in out
        assert "HOME=" not in out

    def test_no_prefix_lists_all(self):
        handler = make_inspect_env("/tmp/c.yml", default_service="backend")
        env = "FOO=1\nBAR=2\n"
        with patch("subprocess.run", return_value=_proc(stdout=env)):
            out = handler({})
        # sorted output
        assert out.index("BAR=") < out.index("FOO=")

    def test_no_match(self):
        handler = make_inspect_env("/tmp/c.yml", default_service="backend")
        with patch("subprocess.run", return_value=_proc(stdout="FOO=1\n")):
            out = handler({"path": "ZZZ"})
        assert "no env vars matching 'ZZZ'" in out


# ── builder ─────────────────────────────────────────────────────────────


class TestBuilder:
    def test_builds_full_set(self):
        handlers = build_container_handlers("/tmp/c.yml", default_service="backend")
        assert set(handlers.keys()) == {
            "tail_logs", "run_in_container", "run_python_in_container",
            "hit_endpoint", "inspect_env",
        }
        for h in handlers.values():
            assert callable(h)

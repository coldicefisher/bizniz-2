"""Storybook dev server lifecycle for ProUXDesigner — Phase 2a.

Spawns ``npm run storybook`` as a subprocess against a frontend
workspace, polls ``/iframe.html`` until ready, yields the base
URL, and tears the server down cleanly on exit (terminate → wait →
kill if needed).

Phase 2a: local subprocess mode (workspace must have node_modules
+ npm on PATH). The next step in roadmap item 2 wires this through
``docker compose exec frontend`` for builds where the frontend
already runs in a container. Until then, this works for local dev
+ tests and serves as the API boundary.

Used as a context manager:

    with StorybookServer(frontend_root) as server:
        capture_stories(catalog, server.base_url, ...)
"""
from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional


# Storybook's CI flag suppresses "open browser" and exit-on-error
# noise. ``--quiet`` cuts the per-file progress logs. Port is
# explicit so we can poll the right URL even if a stray Storybook
# is running elsewhere.
_DEFAULT_PORT = 6006
_READINESS_PATH = "/iframe.html"


class StorybookServerError(RuntimeError):
    """Raised when the server fails to start within the readiness budget."""


class StorybookServer:
    """Context manager wrapping a Storybook dev-server subprocess."""

    def __init__(
        self,
        frontend_root: Path,
        port: int = _DEFAULT_PORT,
        startup_timeout_s: float = 90.0,
        poll_interval_s: float = 1.0,
        on_status: Optional[Callable[[str], None]] = None,
        # Injected for tests; production uses ``subprocess.Popen`` +
        # ``urllib.request.urlopen``.
        spawn: Optional[Callable[..., subprocess.Popen]] = None,
        probe: Optional[Callable[[str], int]] = None,
    ) -> None:
        self._frontend_root = Path(frontend_root).resolve()
        self._port = int(port)
        self._startup_timeout_s = float(startup_timeout_s)
        self._poll_interval_s = float(poll_interval_s)
        self._on_status = on_status
        self._spawn = spawn or self._default_spawn
        self._probe = probe or _default_probe
        self._proc: Optional[subprocess.Popen] = None

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self._port}"

    @property
    def readiness_url(self) -> str:
        return f"{self.base_url}{_READINESS_PATH}"

    # ── Context manager ──────────────────────────────────────────

    def __enter__(self) -> "StorybookServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        if self._proc is not None:
            raise StorybookServerError("StorybookServer.start() called twice")
        self._log(
            f"StorybookServer: starting on port {self._port} "
            f"(timeout {self._startup_timeout_s:.0f}s)..."
        )
        self._proc = self._spawn(self._frontend_root, self._port)
        deadline = time.monotonic() + self._startup_timeout_s
        last_error: Optional[str] = None
        while time.monotonic() < deadline:
            # If the subprocess died, abort instead of waiting out
            # the full readiness budget.
            rc = self._proc.poll()
            if rc is not None:
                self._proc = None
                raise StorybookServerError(
                    f"Storybook subprocess exited with code {rc} during "
                    f"startup (before becoming ready)"
                )
            try:
                status = self._probe(self.readiness_url)
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                status = -1
            if status == 200:
                self._log(
                    f"StorybookServer: ready at {self.base_url}"
                )
                return
            time.sleep(self._poll_interval_s)
        # Timed out — kill the process before raising so we don't
        # leak it.
        self._terminate_process()
        raise StorybookServerError(
            f"Storybook failed to become ready on {self.readiness_url} "
            f"within {self._startup_timeout_s:.0f}s (last probe: {last_error})"
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        self._terminate_process()
        self._proc = None
        self._log("StorybookServer: stopped")

    # ── Internals ────────────────────────────────────────────────

    def _terminate_process(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        try:
            self._proc.terminate()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            self._proc.kill()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _default_spawn(
        self, frontend_root: Path, port: int,
    ) -> subprocess.Popen:
        # ``--ci`` suppresses interactive browser-open and "any
        # console errors → exit non-zero" noise. ``--quiet`` cuts the
        # per-file progress logs. Storybook may not accept positional
        # port via ``--`` in all versions, so we use the env var path.
        return subprocess.Popen(
            ["npm", "run", "storybook", "--",
             "--ci", "--quiet", "--no-open",
             "--port", str(port),
             "--host", "0.0.0.0"],
            cwd=str(frontend_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Storybook spawns children — group them.
        )

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass


def _default_probe(url: str) -> int:
    """Return the HTTP status code, or ``-1`` on connection failure."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.getcode()
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return -1


@contextmanager
def storybook_server(
    frontend_root: Path,
    port: int = _DEFAULT_PORT,
    startup_timeout_s: float = 90.0,
    on_status: Optional[Callable[[str], None]] = None,
):
    """Convenience: ``with storybook_server(root) as s: ...``."""
    server = StorybookServer(
        frontend_root=frontend_root,
        port=port,
        startup_timeout_s=startup_timeout_s,
        on_status=on_status,
    )
    try:
        with server:
            yield server
    finally:
        # Defensive: ``with server`` already stops, but in case the
        # ContextManager protocol gets bypassed.
        server.stop()

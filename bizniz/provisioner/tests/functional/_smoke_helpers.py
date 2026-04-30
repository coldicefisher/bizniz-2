"""Shared helpers for the heavy + free smoke tests.

Both tests share the same "bring up stack, poll endpoints, tear down,
capture diagnostics on failure" core. Only the way they obtain the
``SystemArchitecture`` differs (real Gemini call vs. hand-crafted dict).
"""
from __future__ import annotations

import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


# ── Skip helpers ─────────────────────────────────────────────────────────────


def ensure_docker():
    if shutil.which("docker") is None:
        pytest.skip("docker not in PATH — skipping smoke test")
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            pytest.skip("docker daemon not responsive — skipping smoke test")
    except Exception as e:
        pytest.skip(f"docker daemon check failed ({e}) — skipping smoke test")


# ── Compose helpers ──────────────────────────────────────────────────────────


def compose(
    compose_path: Path, *args: str, timeout: int = 60,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(compose_path), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def capture_diagnostics(compose_path: Path, log_dir: Path) -> None:
    """Best-effort: dump compose ps + logs on failure for debugging."""
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        ps = compose(compose_path, "ps", "-a")
        (log_dir / "compose-ps.txt").write_text(
            ps.stdout + "\n--- STDERR ---\n" + ps.stderr,
        )
    except Exception as e:
        (log_dir / "compose-ps.txt").write_text(f"ps failed: {e}")
    try:
        logs = compose(compose_path, "logs", "--no-color", "--tail=200", timeout=60)
        (log_dir / "compose-logs.txt").write_text(
            logs.stdout + "\n--- STDERR ---\n" + logs.stderr,
        )
    except Exception as e:
        (log_dir / "compose-logs.txt").write_text(f"logs failed: {e}")


def compose_down(compose_path: Path) -> None:
    """Always-runs cleanup. Removes containers, volumes, and the
    project's own images. Best effort — never raises from cleanup."""
    try:
        compose(
            compose_path, "down", "-v", "--rmi", "all", "--remove-orphans",
            timeout=180,
        )
    except Exception:
        pass


# ── HTTP polling ─────────────────────────────────────────────────────────────


def http_alive(url: str, timeout: float, expect_ok: bool = False) -> bool:
    """Poll GET <url> until it returns any HTTP response, or timeout.

    With ``expect_ok=True``, only 2xx counts as success — useful for
    services like FusionAuth where we want to know it's actually
    initialized, not just listening.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if expect_ok:
                    if 200 <= resp.status < 300:
                        return True
                else:
                    return True  # any HTTP response is "alive"
        except urllib.error.HTTPError:
            if not expect_ok:
                return True  # 4xx/5xx still means the server is up
        except Exception:
            pass
        time.sleep(2)
    return False


# ── Image inventory ──────────────────────────────────────────────────────────


def image_present(image_tag: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image_tag],
            capture_output=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False

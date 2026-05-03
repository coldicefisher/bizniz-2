"""Post-provisioning stack validation.

Brings the stack up, health-checks every service, captures logs
on failure, and tears down. The caller (Architect) dispatches the
debugger if validation fails.

This is the gate between "files on disk + images built" and
"engineering can start." If the stack doesn't come up, the
debugger patches infrastructure files (Dockerfile, compose,
init.sql, kickstart.json) before any AI writes application code.
"""
from __future__ import annotations

import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture


@dataclass
class ServiceHealth:
    """Health check result for one service."""
    name: str
    healthy: bool
    check_type: str  # "http", "tcp", "none"
    logs: str = ""   # container logs if unhealthy


@dataclass
class StackValidation:
    """Result of validating the full stack."""
    healthy: bool
    services: List[ServiceHealth] = field(default_factory=list)
    compose_path: str = ""

    @property
    def unhealthy_services(self) -> List[ServiceHealth]:
        return [s for s in self.services if not s.healthy]

    def failure_summary(self) -> str:
        """Format unhealthy services for the debugger."""
        lines = []
        for s in self.unhealthy_services:
            lines.append(f"=== {s.name} ({s.check_type} check FAILED) ===")
            if s.logs:
                # Last 40 lines of logs
                log_tail = "\n".join(s.logs.splitlines()[-40:])
                lines.append(log_tail)
            lines.append("")
        return "\n".join(lines)


# Health check configs by service type
_HEALTH_CHECKS = {
    "backend": {"type": "http", "path": "/openapi.json"},
    "frontend": {"type": "http", "path": "/"},
    "database": {"type": "tcp"},
    "auth": {"type": "http", "path": "/api/status"},
    "cache": {"type": "tcp"},
}


def _log(on_status: Optional[Callable[[str], None]], msg: str) -> None:
    if on_status:
        on_status(msg)


def _wait_http(url: str, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(2.0)
    return False


def _wait_tcp(host: str, port: int, timeout_s: float) -> bool:
    import socket
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            with socket.create_connection((host, port), timeout=3.0):
                return True
        except Exception:
            pass
        time.sleep(2.0)
    return False


def _capture_logs(compose_path: str, service_name: str) -> str:
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", compose_path, "logs",
             "--no-color", "--tail", "60", service_name],
            capture_output=True, text=True, timeout=30,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:
        return f"(could not read logs: {e})"


def teardown_stack(
    compose_path: str,
    on_status: Optional[Callable[[str], None]] = None,
) -> None:
    """Tear down the compose stack."""
    _log(on_status, "Stack: tearing down...")
    try:
        subprocess.run(
            ["docker", "compose", "-f", compose_path, "down"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        _log(on_status, f"Stack: teardown error ({e})")


def validate_stack(
    architecture: SystemArchitecture,
    compose_path: str,
    on_status: Optional[Callable[[str], None]] = None,
    service_timeout_s: float = 60.0,
    port_remap: Optional[Dict[str, tuple]] = None,
    teardown: bool = True,
) -> StackValidation:
    """Bring the stack up, health-check every service, optionally tear down.

    Returns a StackValidation with per-service health status and
    logs for any unhealthy services.
    """
    _log(on_status, "Stack validation: bringing up all services...")

    # Bring up
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", compose_path, "up", "-d"],
            capture_output=True, text=True, timeout=240,
        )
        if proc.returncode != 0:
            _log(on_status, f"Stack validation: compose up failed (rc={proc.returncode})")
            return StackValidation(
                healthy=False,
                compose_path=compose_path,
                services=[ServiceHealth(
                    name="(compose)",
                    healthy=False,
                    check_type="compose_up",
                    logs=(proc.stdout or "") + (proc.stderr or ""),
                )],
            )
    except subprocess.TimeoutExpired:
        return StackValidation(
            healthy=False,
            compose_path=compose_path,
            services=[ServiceHealth(
                name="(compose)",
                healthy=False,
                check_type="compose_up",
                logs="docker compose up timed out after 240s",
            )],
        )

    # Wait a moment for services to initialize
    time.sleep(3)

    # Health check each service
    results = []
    all_healthy = True

    for svc in architecture.services:
        check_config = _HEALTH_CHECKS.get(svc.service_type, {"type": "none"})
        check_type = check_config["type"]

        if check_type == "none" or not svc.port:
            results.append(ServiceHealth(
                name=svc.name, healthy=True, check_type="skip",
            ))
            continue

        # Use the host port (after remapping if applicable)
        host_port = svc.port
        if port_remap and svc.name in port_remap:
            host_port = port_remap[svc.name][1]  # (old, new)

        _log(on_status, f"Stack validation: checking '{svc.name}' ({check_type} on port {host_port})...")

        if check_type == "http":
            path = check_config.get("path", "/")
            url = f"http://localhost:{host_port}{path}"
            healthy = _wait_http(url, timeout_s=service_timeout_s)
        elif check_type == "tcp":
            healthy = _wait_tcp("localhost", host_port, timeout_s=service_timeout_s)
        else:
            healthy = True

        logs = ""
        if not healthy:
            all_healthy = False
            logs = _capture_logs(compose_path, svc.name)
            _log(on_status, f"Stack validation: '{svc.name}' UNHEALTHY")
        else:
            _log(on_status, f"Stack validation: '{svc.name}' healthy")

        results.append(ServiceHealth(
            name=svc.name, healthy=healthy,
            check_type=check_type, logs=logs,
        ))

    # Tear down — clean state for engineering (unless caller needs
    # the stack up for further provisioning like FusionAuth setup)
    if teardown:
        teardown_stack(compose_path, on_status)

    status = "HEALTHY" if all_healthy else "UNHEALTHY"
    _log(on_status, f"Stack validation: {status} ({len(results)} services checked)")

    return StackValidation(
        healthy=all_healthy,
        services=results,
        compose_path=compose_path,
    )

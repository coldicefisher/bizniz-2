"""Capture backend contracts (OpenAPI specs) from a built stack.

After backend services pass their unit/orchestrator tests, we need
their actual exposed API surface to (1) hand to downstream frontend
engineers as a contract and (2) feed into the integration tester so
it knows what endpoints to exercise. The cheap way is to spin up
each backend, hit ``/openapi.json``, save, and stop. ~30s per
backend.

Failure mode handling: if a backend can't be reached (compose-up
failed, port collision, app crash on import), this returns a
partial dict with the captured services. The caller decides whether
to fail the run or continue with what we got.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.architect.types import ServiceDefinition, SystemArchitecture


def _log(on_status: Optional[Callable[[str], None]], msg: str) -> None:
    if on_status:
        on_status(msg)


def _http_get_json(url: str, timeout: float = 5.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _wait_for_openapi(port: int, deadline_s: float) -> Optional[dict]:
    end = time.monotonic() + deadline_s
    url = f"http://localhost:{port}/openapi.json"
    while time.monotonic() < end:
        doc = _http_get_json(url, timeout=3.0)
        if doc is not None and isinstance(doc.get("paths"), dict):
            return doc
        time.sleep(2.0)
    return None


def _resolve_host_port(
    compose_path: str, service_name: str, container_port: int,
) -> int:
    """Ask docker compose for the host port bound to
    ``<service_name>:<container_port>``. Falls back to ``container_port``
    if the query fails. Same approach SmokePhase uses — architecture
    ports are container ports; host bindings may differ when
    provisioner remaps to avoid collisions."""
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", compose_path,
             "port", service_name, str(container_port)],
            capture_output=True, text=True, timeout=10,
        )
        out = (result.stdout or "").strip()
        if result.returncode == 0 and ":" in out:
            return int(out.rsplit(":", 1)[1])
    except Exception:
        pass
    return container_port


def _backends_with_ports(
    architecture: SystemArchitecture,
    only_names: Optional[List[str]] = None,
) -> List[ServiceDefinition]:
    backends = [
        s for s in architecture.services
        if s.service_type == "backend" and s.port
    ]
    if only_names is not None:
        wanted = set(only_names)
        backends = [s for s in backends if s.name in wanted]
    return backends


def capture_backend_contracts(
    architecture: SystemArchitecture,
    project_root: Path,
    compose_path: str,
    on_status: Optional[Callable[[str], None]] = None,
    only_names: Optional[List[str]] = None,
    backend_wait_s: float = 60.0,
) -> Dict[str, dict]:
    """Spin up backends, capture each ``/openapi.json``, write to
    ``<project_root>/contracts/<service>.openapi.json``, stop them.

    Returns ``{service_name: openapi_doc}`` for every backend whose
    spec was successfully captured. Backends that don't respond are
    silently omitted; the caller decides whether absence is fatal.

    ``only_names`` restricts capture to a subset (useful between
    layers, when only a subset has been built).
    """
    backends = _backends_with_ports(architecture, only_names=only_names)
    if not backends:
        return {}

    compose_path = str(compose_path)
    contracts_dir = Path(project_root) / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)

    backend_names = [s.name for s in backends]
    _log(
        on_status,
        f"Contracts: bringing up {len(backends)} backend(s) "
        f"({', '.join(backend_names)}) to capture OpenAPI..."
    )

    up = subprocess.run(
        ["docker", "compose", "-f", compose_path, "up", "-d", *backend_names],
        capture_output=True, text=True, timeout=240,
    )
    if up.returncode != 0:
        _log(
            on_status,
            f"Contracts: compose up failed (rc={up.returncode}); "
            f"capturing nothing. stderr: {up.stderr.strip()[:300]}"
        )
        try:
            subprocess.run(
                ["docker", "compose", "-f", compose_path, "stop", *backend_names],
                capture_output=True, text=True, timeout=120,
            )
        except Exception:
            pass
        return {}

    captured: Dict[str, dict] = {}
    try:
        for backend in backends:
            host_port = _resolve_host_port(
                compose_path, backend.name, backend.port,
            )
            doc = _wait_for_openapi(host_port, deadline_s=backend_wait_s)
            if doc is None:
                _log(
                    on_status,
                    f"Contracts: '{backend.name}' did not expose /openapi.json "
                    f"on :{host_port} within {backend_wait_s:.0f}s — skipping"
                )
                continue
            captured[backend.name] = doc
            out_path = contracts_dir / f"{backend.name}.openapi.json"
            out_path.write_text(json.dumps(doc, indent=2))
            paths_count = len(doc.get("paths", {}))
            _log(
                on_status,
                f"Contracts: '{backend.name}' captured "
                f"({paths_count} paths) → {out_path.relative_to(project_root)}"
            )
    finally:
        _log(on_status, f"Contracts: stopping {len(backends)} backend(s)...")
        try:
            subprocess.run(
                ["docker", "compose", "-f", compose_path, "stop", *backend_names],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            _log(on_status, f"Contracts: stop error ({e})")

    return captured


def load_contract(project_root: Path, service_name: str) -> Optional[dict]:
    """Read a previously-captured contract, or None."""
    path = Path(project_root) / "contracts" / f"{service_name}.openapi.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

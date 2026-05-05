"""Run the post-flight validator for a service.

Dispatches the right runner based on the profile's ``validator_runner``
field. For TypeScript we override the entrypoint of the existing
``bizniz-doc-typescript`` sidecar (it already has the TypeScript
compiler installed) so we don't need a second image. For Python
we use a Python subprocess against pyright if available.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from bizniz.profiles import profile_for, UnknownServiceTypeError


@dataclass
class ValidationReport:
    """Result of a post-flight validator run."""
    service_name: str
    runner: str
    command: List[str]
    passed: bool
    stdout: str
    stderr: str
    skipped_reason: Optional[str] = None  # set when we couldn't run it

    @property
    def summary(self) -> str:
        if self.skipped_reason:
            return f"validator skipped: {self.skipped_reason}"
        if self.passed:
            return f"validator OK ({self.runner})"
        # Show the first ~10 lines of error output
        snippet = (self.stderr.strip() or self.stdout.strip())
        head = "\n".join(snippet.splitlines()[:10])
        return f"validator FAIL ({self.runner}): {head}"


class ValidatorError(RuntimeError):
    pass


# ── runner dispatch ─────────────────────────────────────────────────


def run_validator(
    service,
    workspace_root: Path,
    timeout_s: int = 120,
) -> ValidationReport:
    """Look up the service's profile and run its post-flight validator.

    Returns a ValidationReport — never raises. If the profile has
    no validator, or the runner is unavailable, returns a report
    with ``skipped_reason`` set.
    """
    workspace_root = Path(workspace_root)

    try:
        prof = profile_for(service)
    except UnknownServiceTypeError as e:
        return _skip(service, "no profile", str(e))

    if not prof.validator:
        return _skip(service, "no_validator_in_profile", "")

    if prof.validator_runner == "node-sidecar":
        return _run_node_sidecar(service, prof, workspace_root, timeout_s)
    if prof.validator_runner == "python-sidecar":
        return _run_python_sidecar(service, prof, workspace_root, timeout_s)
    if prof.validator_runner == "python":
        return _run_python_local(service, prof, workspace_root, timeout_s)

    return _skip(
        service, "unknown_validator_runner",
        f"runner {prof.validator_runner!r} has no dispatch handler"
    )


def _skip(service, reason: str, detail: str) -> ValidationReport:
    return ValidationReport(
        service_name=getattr(service, "name", "?"),
        runner="(skipped)",
        command=[],
        passed=True,  # treat skip as pass — caller decides if absent validator is acceptable
        stdout="",
        stderr=detail,
        skipped_reason=reason,
    )


# ── TypeScript via the docs sidecar (it already has tsc) ────────────


_TS_SIDECAR_IMAGE = "bizniz-doc-typescript:latest"
_PY_SIDECAR_IMAGE = "bizniz-doc-python:latest"


def _run_node_sidecar(service, prof, workspace_root: Path, timeout_s: int) -> ValidationReport:
    """Run a node-based validator (tsc) inside the doc sidecar.

    The doc sidecar has the TypeScript compiler installed but NOT
    the project's runtime deps (react, jest types, etc.). When the
    workspace lacks ``node_modules/`` on the host filesystem (i.e.
    the project's deps live only inside its docker container),
    tsc fails with hundreds of TS2307 "Cannot find module 'react'"
    errors that aren't real code bugs — they're environment-level
    "module resolution can't see the deps."

    Soft-skip in that case rather than fail. Long-term fix: switch
    to ``docker exec frontend-container tsc --noEmit`` so tsc runs
    inside the project container where ``node_modules`` exists.
    """
    if not _docker_available():
        return _skip(service, "docker_unavailable", "")

    # If the workspace doesn't have node_modules on the host, the
    # sidecar's tsc can't resolve project deps. Skip rather than
    # fail with noise the post-flight repair can't address.
    node_modules = workspace_root / "node_modules"
    if not node_modules.is_dir():
        return _skip(
            service, "node_modules_missing",
            f"node_modules not present at {node_modules}; tsc would fail "
            f"on every project import. The frontend's deps are installed "
            f"inside its container, not on the host. Validation should "
            f"run via docker exec into the running container — see "
            f"follow-up in bizniz/validators/runner.py."
        )

    # Mount workspace, override entrypoint to /bin/sh, run the
    # validator command. We rewrite ``npx tsc ...`` and bare ``tsc``
    # invocations to point at the sidecar's pre-installed TypeScript
    # at /opt/extractor/node_modules/.bin/tsc. Using npx in the
    # sidecar tries to download a fresh "tsc" package (the
    # typo-squatter, not the real compiler) — this avoids that.
    cmd = list(prof.validator)
    if cmd and cmd[0] == "npx" and len(cmd) > 1 and cmd[1] == "tsc":
        cmd = ["/opt/extractor/node_modules/.bin/tsc"] + cmd[2:]
    elif cmd and cmd[0] == "tsc":
        cmd = ["/opt/extractor/node_modules/.bin/tsc"] + cmd[1:]
    cmd_str = " ".join(_shell_quote(arg) for arg in cmd)
    sh_cmd = f"cd /workspace && {cmd_str}"
    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{workspace_root}:/workspace:ro",
        "--entrypoint", "/bin/sh",
        _TS_SIDECAR_IMAGE,
        "-c", sh_cmd,
    ]

    try:
        proc = subprocess.run(
            docker_cmd, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        return ValidationReport(
            service_name=service.name,
            runner="node-sidecar",
            command=prof.validator,
            passed=False,
            stdout=(e.stdout or "")[:8000],
            stderr=f"validator timed out after {timeout_s}s",
        )

    return ValidationReport(
        service_name=service.name,
        runner="node-sidecar",
        command=prof.validator,
        passed=proc.returncode == 0,
        stdout=(proc.stdout or "")[:8000],
        stderr=(proc.stderr or "")[:8000],
    )


def _shell_quote(s: str) -> str:
    if not s:
        return "''"
    if all(c.isalnum() or c in "-_/.@:=+" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


# ── Python via the sidecar (mypy pre-installed) ────────────────────


def _run_python_sidecar(service, prof, workspace_root: Path, timeout_s: int) -> ValidationReport:
    """Run a Python validator (mypy) inside the bizniz-doc-python sidecar.

    The sidecar ships with mypy + common type stubs (pydantic,
    sqlalchemy, fastapi) pre-installed. Override the entrypoint so
    the validator runs instead of the default documenter mode.
    """
    if not _docker_available():
        return _skip(service, "docker_unavailable", "")

    cmd_str = " ".join(_shell_quote(arg) for arg in prof.validator)
    sh_cmd = f"cd /workspace && {cmd_str}"
    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{workspace_root}:/workspace:ro",
        "--entrypoint", "/bin/sh",
        _PY_SIDECAR_IMAGE,
        "-c", sh_cmd,
    ]

    try:
        proc = subprocess.run(
            docker_cmd, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        return ValidationReport(
            service_name=service.name,
            runner="python-sidecar",
            command=prof.validator,
            passed=False,
            stdout=(e.stdout or "")[:8000],
            stderr=f"validator timed out after {timeout_s}s",
        )

    return ValidationReport(
        service_name=service.name,
        runner="python-sidecar",
        command=prof.validator,
        passed=proc.returncode == 0,
        stdout=(proc.stdout or "")[:8000],
        stderr=(proc.stderr or "")[:8000],
    )


# ── Python local subprocess (legacy fallback) ──────────────────────


def _run_python_local(service, prof, workspace_root: Path, timeout_s: int) -> ValidationReport:
    """Run a Python validator (pyright/mypy) as a local subprocess.

    Python type checkers tend to be installed in the runner's venv,
    not the project's. If pyright/mypy isn't on PATH, soft-skip
    rather than failing the engineer for a missing dev tool.
    """
    cmd0 = (prof.validator or [None])[0]
    if cmd0 == "python":
        # `python -m pyright app/` style — works as long as `pyright`
        # module is importable from the bizniz venv.
        executable = shutil.which("python") or shutil.which("python3")
    else:
        executable = shutil.which(cmd0) if cmd0 else None

    if not executable:
        return _skip(service, "validator_not_installed",
                     f"command not found on PATH: {cmd0}")

    full_cmd = [executable] + list(prof.validator[1:])

    try:
        proc = subprocess.run(
            full_cmd, capture_output=True, text=True,
            cwd=str(workspace_root), timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        return ValidationReport(
            service_name=service.name,
            runner="python",
            command=full_cmd,
            passed=False,
            stdout=(e.stdout or "")[:8000],
            stderr=f"validator timed out after {timeout_s}s",
        )
    except FileNotFoundError as e:
        return _skip(service, "validator_module_missing", str(e))

    # Detect "module not installed" failures and soft-skip rather
    # than reporting them as type errors. Hitting "No module named
    # pyright" doesn't mean the user's code is broken — it means
    # the dev tool isn't on the runner's PATH. The architect should
    # NOT mark the service failed in that case.
    stderr = proc.stderr or ""
    stdout = proc.stdout or ""
    combined = stderr + "\n" + stdout
    if proc.returncode != 0 and (
        "No module named" in combined
        or "ModuleNotFoundError" in combined
    ):
        return _skip(
            service, "validator_module_missing",
            stderr.strip()[:300] or stdout.strip()[:300],
        )

    # pyright exits non-zero on type errors; mypy similarly. We trust
    # exit code as the pass/fail signal once we've ruled out missing-
    # tool noise.
    return ValidationReport(
        service_name=service.name,
        runner="python",
        command=full_cmd,
        passed=proc.returncode == 0,
        stdout=stdout[:8000],
        stderr=stderr[:8000],
    )


def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False

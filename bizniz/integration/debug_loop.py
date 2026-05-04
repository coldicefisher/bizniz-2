"""Agentic debug loop for failed integration tests.

When the pytest sidecar exits non-zero, dispatch the AgenticDebugger
against the failure. It has discovery tools (view_file,
list_directory, search_files, run_command) so it can explore the
service workspace, find the bug, and propose direct code fixes.
We apply the fixes, re-run pytest in the sidecar, and loop up to
``max_iterations`` times before giving up.

Why this lives in ``bizniz.integration`` and not in the orchestrator:
the orchestrator's debug loop fires on UNIT-test failures and only
sees mocked-DB / single-service code. Integration failures need the
LIVE stack present (Postgres up, real HTTP), the real source tree
(no mocks), and the AI to think across the boundary between code
and runtime infrastructure (table creation, env vars, schema
migrations). Same agent class, different harness.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Tuple

from bizniz.architect.types import ServiceDefinition


def _log(on_status: Optional[Callable[[str], None]], msg: str) -> None:
    if on_status:
        on_status(msg)


def _read_workspace_files(
    workspace,
    rel_paths: list[str],
) -> dict[str, str]:
    out = {}
    for rel in rel_paths:
        try:
            p = workspace.path(rel)
            if p.is_file():
                out[rel] = p.read_text()
        except Exception:
            pass
    return out


# Project manifests: always include these if they exist — they're
# small and tell the debugger what's installed and how the build works.
_MANIFEST_FILES = frozenset({
    "package.json", "requirements.txt", "tsconfig.json",
    "vite.config.ts", "pyproject.toml", "setup.cfg",
})

# Lockfiles / noise: never include these — they're huge and
# add no diagnostic value.
_SKIP_FILES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock",
})


def _list_relevant_source_files(workspace, max_files: int = 30) -> list[str]:
    """Return up to ``max_files`` source files the debugger can use as
    initial context. The agent has discovery tools to fetch more on
    demand; this is just the first window."""
    try:
        relatives = workspace.list_relative_files()
    except Exception:
        return []

    # Always include manifests first — they're small and critical.
    manifests = []
    source = []
    for rel in relatives:
        s = str(rel)
        basename = s.rsplit("/", 1)[-1] if "/" in s else s
        if basename in _SKIP_FILES:
            continue
        if any(skip in s for skip in (
            "__pycache__", ".pytest_cache", "node_modules",
            ".bizniz/", "docs/", "contracts/", ".egg-info",
            "coverage/", ".git/",
        )):
            continue
        if basename in _MANIFEST_FILES:
            manifests.append(s)
        elif s.startswith(("app/", "src/")) or s.endswith((".py", ".ts", ".tsx")):
            source.append(s)

    # Manifests first, then source up to the cap.
    keep = manifests
    remaining = max_files - len(keep)
    if remaining > 0:
        keep.extend(source[:remaining])
    return keep


def _is_hallucinated_new_file(
    filepath: str,
    new_content: str,
    workspace,
    problem_statement: str,
) -> bool:
    """Return True if the debugger is touching a file whose path
    contains a domain noun absent from the problem statement (e.g.
    ``app/api/routes/grooming.py`` in a property-manager project).

    We reject BOTH creation and modification of such files. A naive
    "only block new files" check let prior corruption persist through
    re-runs — the file existed from a previous bad run, so subsequent
    debugger fixes were allowed to keep editing it.
    """
    from bizniz.integration.hallucination_guard import _tokenize, _GENERIC_VOCAB
    from pathlib import Path as _Path
    import re as _re

    # Walk EVERY directory part + filename stem of the path. Any of
    # them having a domain-suspicious word is grounds to reject.
    p = _Path(filepath)
    candidates = list(p.parts[:-1]) + [p.stem]

    problem_tokens = _tokenize(problem_statement)
    allowed = problem_tokens | _GENERIC_VOCAB

    for cand in candidates:
        # Split CamelCase, snake_case, kebab-case, and dotted paths so
        # all of these get checked piece-by-piece:
        #   GroomingPage     → grooming, page
        #   appointments_router → appointments, router
        #   user-profile     → user, profile
        #   vite.config      → vite, config
        for piece in _re.split(r"[._-]|(?<=[a-z])(?=[A-Z])", cand):
            piece = piece.lower()
            if len(piece) < 5:
                continue  # skip short tokens (avoid noise from "src", "app", "vite")
            if piece in allowed:
                continue
            return True
    return False


def _load_auth_contract(workspace, compose_path: Optional[str]) -> Optional[str]:
    """Try to find AUTH_CONTRACT.md at the project root.

    The project root is the parent of the service workspace, or
    derivable from the compose path (infra/development/docker-compose.yml
    → project root is two levels up).
    """
    candidates = []

    # From workspace: go up to project root
    ws_root = Path(workspace.root) if hasattr(workspace, "root") else None
    if ws_root:
        # Service workspace is <project_root>/<service_name>/
        candidates.append(ws_root.parent / "AUTH_CONTRACT.md")

    # From compose path: <project_root>/infra/development/docker-compose.yml
    if compose_path:
        candidates.append(Path(compose_path).parent.parent.parent / "AUTH_CONTRACT.md")

    for path in candidates:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
        except Exception:
            continue
    return None


def repair_integration_failure(
    *,
    service: ServiceDefinition,
    workspace,
    failure_output: str,
    integration_test_rel: str,
    debugger_factory: Callable,
    rerun_tests: Callable[[], Tuple[bool, str]],
    on_status: Optional[Callable[[str], None]] = None,
    max_iterations: int = 3,
    capture_logs: Optional[Callable[[], str]] = None,
    compose_path: Optional[str] = None,
    problem_statement: Optional[str] = None,
) -> Tuple[bool, str]:
    """Run the agentic debug loop. Returns ``(passed, final_output)``.

    ``rerun_tests`` is a closure the caller provides that re-executes
    the pytest sidecar against the now-modified workspace and returns
    ``(passed, output)``. Keeping it as a callback means this module
    doesn't have to know about docker; the integration runner does.

    ``capture_logs`` is an optional closure that returns the container's
    recent log output (e.g., docker compose logs). When provided, the
    logs are prepended to the error output so the debugger can see
    server-side tracebacks, not just client-side assertion failures.
    """
    last_output = failure_output
    repair_history: list[str] = []

    # If an AUTH_CONTRACT.md exists at the project root, prepend it to
    # the error context. This tells the debugger: FusionAuth is the
    # source of truth for auth, and skeleton auth files (auth.py,
    # routes/auth.py) MAY be modified to match the contract.
    auth_contract = _load_auth_contract(workspace, compose_path)
    if auth_contract:
        last_output = (
            f"=== AUTH CONTRACT (FusionAuth is configured — skeleton auth files "
            f"MAY be modified to match this contract) ===\n"
            f"{auth_contract}\n\n"
            f"{last_output}"
        )

    # Capture server-side logs on the initial failure so the debugger
    # sees both "assert 422 == 200" AND the server's traceback.
    if capture_logs is not None:
        try:
            server_logs = capture_logs()
            if server_logs and server_logs.strip():
                # Tail to avoid overwhelming the context — last 60 lines
                # covers most startup + request-handling tracebacks.
                tail = "\n".join(server_logs.splitlines()[-60:])
                last_output = (
                    f"=== Server logs ({service.name}, last 60 lines — use inspect_container for more) ===\n"
                    f"{tail}\n\n"
                    f"=== Test output ===\n"
                    f"{last_output}"
                )
        except Exception:
            pass

    for iteration in range(1, max_iterations + 1):
        _log(
            on_status,
            f"Integration debug: '{service.name}' iteration {iteration}/{max_iterations} — "
            f"running AgenticDebugger..."
        )

        # Initial source context: source files in the workspace, plus
        # the integration test we're trying to satisfy.
        source_files = _read_workspace_files(
            workspace, _list_relevant_source_files(workspace),
        )
        test_files = _read_workspace_files(workspace, [integration_test_rel])

        try:
            debugger = debugger_factory()
            # Arm the debugger with container inspection if we have
            # a compose path. This lets it pull more logs or exec
            # commands inside the running container on demand.
            if compose_path and hasattr(debugger, '_compose_path'):
                debugger._compose_path = compose_path
                debugger._service_name = service.name
            diagnosis = debugger.diagnose(
                error_output=last_output,
                source_files=source_files,
                test_files=test_files,
                repair_history=repair_history,
            )
        except Exception as e:
            _log(on_status, f"Integration debug: diagnose raised ({type(e).__name__}: {e}) — giving up")
            return False, last_output

        _log(
            on_status,
            f"Integration debug: '{service.name}' diagnosis — "
            f"{diagnosis.root_cause_category}, fix_target={diagnosis.fix_target}, "
            f"{len(diagnosis.code_fixes)} direct fix(es)"
        )

        if not diagnosis.code_fixes:
            # The debugger surfaced a diagnosis but no code fixes.
            # Without applicable fixes we can't iterate; the diagnosis
            # itself is useful evidence for the human reviewer.
            _log(
                on_status,
                f"Integration debug: '{service.name}' debugger produced no code fixes; "
                f"escalating to human."
            )
            return False, (
                f"{last_output}\n\n"
                f"=== AgenticDebugger diagnosis (no auto-fix available) ===\n"
                f"Root cause: {diagnosis.root_cause_category}\n"
                f"Diagnosis: {diagnosis.diagnosis}\n"
                f"Fix plan: {chr(10).join('  - ' + s for s in diagnosis.fix_plan)}\n"
            )

        # Apply fixes — but reject any new file whose path is rooted
        # in a domain absent from the problem statement. This is the
        # last line of defense against the debugger creating
        # `grooming.py` to satisfy a hallucinated test (we've seen
        # exactly this corruption on property-management projects).
        for fix in diagnosis.code_fixes:
            if problem_statement and _is_hallucinated_new_file(
                fix.filepath, fix.new_content, workspace, problem_statement,
            ):
                _log(
                    on_status,
                    f"Integration debug: REJECTED fix to {fix.filepath} — "
                    f"path/content references a domain not in the problem "
                    f"statement (likely amplifying a hallucinated test)."
                )
                continue
            try:
                workspace.write_file(path=fix.filepath, content=fix.new_content)
                _log(on_status, f"Integration debug: applied fix to {fix.filepath}")
            except Exception as e:
                _log(on_status, f"Integration debug: failed to apply fix to {fix.filepath}: {e}")

        repair_history.append(
            f"Iteration {iteration}: {diagnosis.diagnosis[:200]}"
        )

        # Re-run integration tests
        _log(on_status, f"Integration debug: '{service.name}' re-running pytest sidecar...")
        passed, output = rerun_tests()

        # Prepend fresh server logs so the next iteration sees updated
        # tracebacks (the container was restarted with new code).
        if not passed and capture_logs is not None:
            try:
                server_logs = capture_logs()
                if server_logs and server_logs.strip():
                    tail = "\n".join(server_logs.splitlines()[-60:])
                    output = (
                        f"=== Server logs ({service.name}, last 60 lines — use inspect_container for more) ===\n"
                        f"{tail}\n\n"
                        f"=== Test output ===\n"
                        f"{output}"
                    )
            except Exception:
                pass
        last_output = output

        if passed:
            _log(
                on_status,
                f"Integration debug: '{service.name}' PASS after {iteration} repair iteration(s)"
            )
            return True, output

        _log(
            on_status,
            f"Integration debug: '{service.name}' iteration {iteration} did not fix; trying again."
        )

    _log(
        on_status,
        f"Integration debug: '{service.name}' did not converge after {max_iterations} iterations"
    )
    return False, last_output

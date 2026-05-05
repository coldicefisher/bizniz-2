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

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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


@dataclass
class DebuggerTierSpec:
    """One tier of the AgenticDebugger escalation chain.

    ``factory`` takes the service's workspace and returns a fresh
    debugger instance for this tier. Each tier may bind a different
    model (cheap-and-many at the start of the chain, expensive-and-
    few at the top). ``model_label`` is the human-readable model
    name used in logs and the sticky repair log.
    """
    factory: Callable                  # (workspace) → AgenticDebugger
    model_label: str
    max_turns: int
    repair_attempts: int


def repair_integration_failure(
    *,
    service: ServiceDefinition,
    workspace,
    failure_output: str,
    integration_test_rel: str,
    debugger_factory: Optional[Callable] = None,
    rerun_tests: Callable[[], Tuple[bool, str]],
    on_status: Optional[Callable[[str], None]] = None,
    max_iterations: int = 3,
    capture_logs: Optional[Callable[[], str]] = None,
    compose_path: Optional[str] = None,
    problem_statement: Optional[str] = None,
    escalation: Optional[List[DebuggerTierSpec]] = None,
) -> Tuple[bool, str]:
    """Run the agentic debug loop with optional escalation. Returns
    ``(passed, final_output)``.

    Two ways to drive this:

    1. ``escalation``: a list of DebuggerTierSpec. The loop runs each
       tier's repair_attempts attempts with that tier's max_turns;
       if a tier exhausts its attempts without converging, escalates
       to the next tier. Every attempt at every tier reads the
       sticky repair log so it doesn't repeat fixes.

    2. ``debugger_factory`` + ``max_iterations`` (legacy): wrapped
       internally as a single-tier escalation. Preserved so older
       callers don't need to change.

    ``rerun_tests`` is a closure the caller provides that re-executes
    the pytest sidecar against the now-modified workspace and returns
    ``(passed, output)``. Keeping it as a callback means this module
    doesn't have to know about docker; the integration runner does.

    ``capture_logs`` is an optional closure that returns the container's
    recent log output (e.g., docker compose logs). When provided, the
    logs are prepended to the error output so the debugger can see
    server-side tracebacks, not just client-side assertion failures.
    """
    # Normalize legacy single-tier callers into the escalation shape.
    if escalation is None:
        if debugger_factory is None:
            raise ValueError(
                "repair_integration_failure: must provide either "
                "`escalation` or `debugger_factory`"
            )
        # Wrap legacy single-arg factory: caller's debugger_factory
        # was already a no-arg callable (workspace pre-bound). Adapt
        # to (ws → debugger) shape we now use uniformly.
        def _legacy_adapter(ws=None, _f=debugger_factory):
            try:
                return _f(ws) if ws is not None else _f()
            except TypeError:
                return _f()
        escalation = [DebuggerTierSpec(
            factory=_legacy_adapter,
            model_label="(unspecified)",
            max_turns=12,
            repair_attempts=max_iterations,
        )]

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

    # Sticky repair log: every attempt at every tier reads the full
    # history. The log lives at <workspace>/.bizniz_repair_log.json
    # so it survives across debugger instantiations and even across
    # tier escalations (flash-top → pro). Readers see what's already
    # been tried and can avoid repeating fixes.
    from bizniz.repair_log import (
        RepairLogEntry as _LogEntry,
        append_entry as _log_append,
        format_for_prompt as _log_format,
    )
    ws_root_path = Path(workspace.root) if hasattr(workspace, "root") else None

    total_attempt = 0
    for tier_idx, tier in enumerate(escalation):
        _log(
            on_status,
            f"Integration debug: '{service.name}' tier {tier_idx + 1}/{len(escalation)} "
            f"({tier.model_label}, max_turns={tier.max_turns}, "
            f"repair_attempts={tier.repair_attempts})..."
        )

        for tier_attempt in range(1, tier.repair_attempts + 1):
            total_attempt += 1
            _log(
                on_status,
                f"Integration debug: '{service.name}' [{tier.model_label}] "
                f"attempt {tier_attempt}/{tier.repair_attempts}..."
            )

            # Initial source context: source files in the workspace, plus
            # the integration test we're trying to satisfy.
            source_files = _read_workspace_files(
                workspace, _list_relevant_source_files(workspace),
            )
            test_files = _read_workspace_files(workspace, [integration_test_rel])

            # Sticky log → debugger's repair_history. Combines local
            # repair_history (this call's prior attempts) with everything
            # in the persistent log (across tiers / agents).
            sticky_block = ""
            if ws_root_path is not None:
                sticky_block = _log_format(ws_root_path)
            combined_history = list(repair_history)
            if sticky_block:
                combined_history.insert(0, sticky_block)

            try:
                debugger = tier.factory(workspace)
                # Cap the agent's per-call turn budget per the tier config.
                # AgenticDebugger reads ``_max_turns`` in its turn loop;
                # writing to any other name silently leaves the constructor
                # default of 15 in place, which is how an earlier version
                # of this code let pro tier blow past its 8-turn cap.
                if hasattr(debugger, "_max_turns"):
                    debugger._max_turns = tier.max_turns
                # Arm the debugger with container inspection if we have
                # a compose path. This lets it pull more logs or exec
                # commands inside the running container on demand.
                if compose_path and hasattr(debugger, "_compose_path"):
                    debugger._compose_path = compose_path
                    debugger._service_name = service.name
                diagnosis = debugger.diagnose(
                    error_output=last_output,
                    source_files=source_files,
                    test_files=test_files,
                    repair_history=combined_history,
                )
            except Exception as e:
                _log(
                    on_status,
                    f"Integration debug: diagnose raised "
                    f"({type(e).__name__}: {e}) — giving up at tier "
                    f"'{tier.model_label}'"
                )
                if ws_root_path is not None:
                    _log_append(ws_root_path, _LogEntry(
                        agent="agenticdebugger",
                        tier=tier.model_label,
                        attempt=tier_attempt,
                        trigger=last_output[:500],
                        diagnosis=f"diagnose raised: {type(e).__name__}: {e}",
                        outcome="error",
                    ))
                return False, last_output

            _log(
                on_status,
                f"Integration debug: '{service.name}' diagnosis — "
                f"{diagnosis.root_cause_category}, fix_target={diagnosis.fix_target}, "
                f"{len(diagnosis.code_fixes)} direct fix(es)"
            )

            if not diagnosis.code_fixes:
                # No applicable fixes from this attempt. Record and
                # continue to next attempt / tier.
                if ws_root_path is not None:
                    _log_append(ws_root_path, _LogEntry(
                        agent="agenticdebugger",
                        tier=tier.model_label,
                        attempt=tier_attempt,
                        trigger=last_output[:500],
                        diagnosis=diagnosis.diagnosis[:500],
                        outcome="no_fixes",
                    ))
                _log(
                    on_status,
                    f"Integration debug: '{service.name}' [{tier.model_label}] "
                    f"attempt {tier_attempt} produced no fixes — continuing chain"
                )
                continue

            # Apply fixes. Hallucination check moved to post-engineer
            # phase as a single AI-reviewed checkpoint (see
            # bizniz/checks/hallucination_review.py); the path-level
            # guard is gone because hardcoded vocab couldn't keep up
            # with codebases like vehinexa whose domain words are by
            # definition outside any pre-curated list.
            applied_fixes = []
            for fix in diagnosis.code_fixes:
                try:
                    workspace.write_file(path=fix.filepath, content=fix.new_content)
                    _log(on_status, f"Integration debug: applied fix to {fix.filepath}")
                    applied_fixes.append({
                        "file": fix.filepath,
                        "summary": (diagnosis.diagnosis or "")[:120],
                    })
                except Exception as e:
                    _log(
                        on_status,
                        f"Integration debug: failed to apply fix to "
                        f"{fix.filepath}: {e}"
                    )

            repair_history.append(
                f"[{tier.model_label} attempt {tier_attempt}]: "
                f"{diagnosis.diagnosis[:200]}"
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
                            f"=== Server logs ({service.name}, last 60 lines — "
                            f"use inspect_container for more) ===\n"
                            f"{tail}\n\n"
                            f"=== Test output ===\n"
                            f"{output}"
                        )
                except Exception:
                    pass
            last_output = output

            # Append this attempt to the sticky log so future tiers /
            # other debug agents see what was tried + the outcome.
            if ws_root_path is not None:
                _log_append(ws_root_path, _LogEntry(
                    agent="agenticdebugger",
                    tier=tier.model_label,
                    attempt=tier_attempt,
                    trigger=(failure_output or "")[:500],
                    diagnosis=diagnosis.diagnosis[:500],
                    fixes=applied_fixes,
                    outcome="passed" if passed else "still_failing",
                ))

            if passed:
                _log(
                    on_status,
                    f"Integration debug: '{service.name}' PASS after "
                    f"{total_attempt} total attempt(s) "
                    f"(tier '{tier.model_label}', attempt {tier_attempt})"
                )
                return True, output

            _log(
                on_status,
                f"Integration debug: '{service.name}' [{tier.model_label}] "
                f"attempt {tier_attempt} did not fix; "
                f"{'next attempt' if tier_attempt < tier.repair_attempts else 'escalating'}."
            )

    _log(
        on_status,
        f"Integration debug: '{service.name}' did not converge after "
        f"{total_attempt} total attempts across "
        f"{len(escalation)} tier(s)"
    )
    return False, last_output

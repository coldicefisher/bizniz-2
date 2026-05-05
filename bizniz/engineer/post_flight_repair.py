"""Post-flight repair — wraps the existing AgenticDebugger to fix
type-checker failures.

When the architect's post-flight validator (mypy/tsc) fails, we used to
fail the service immediately. That left a class of bugs (sync/async
mismatch, wrong typed argument, missing await) caught but not repaired
— forcing a full re-engineering pass to fix issues the type checker
already pointed at exact lines of.

This module reuses:
  - ``AgenticDebugger`` (the LLM brain — same one integration uses)
  - ``DebuggerTierSpec`` escalation (flash-top → pro)
  - The sticky ``.bizniz_repair_log.json`` (every repair attempt
    persists alongside integration repair attempts, so future sessions
    see the full history)

What's *not* reused: integration-specific framing (auth contract
injection, container log capture, hallucination guard for new files).
Type errors don't introduce new domain files; mypy/tsc reports name
the exact line + the exact wrong type, so the LLM has all the
context it needs from the validator output alone.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional, Tuple

from bizniz.integration.debug_loop import DebuggerTierSpec
from bizniz.repair_log.log import (
    RepairLogEntry as _LogEntry,
    append_entry as _log_append,
    format_for_prompt as _log_format,
)


def _log(on_status: Optional[Callable[[str], None]], msg: str) -> None:
    if on_status:
        on_status(msg)


def _ws_root_path(workspace) -> Optional[Path]:
    """Workspace root, falling back to None if the workspace doesn't
    expose a path() method (some test doubles don't)."""
    try:
        return Path(str(workspace.path("")))
    except Exception:
        return None


def repair_post_flight_failure(
    *,
    service_name: str,
    workspace,
    validator_output: str,
    failing_files: List[str],
    rerun_validator: Callable[[], Tuple[bool, str]],
    escalation: List[DebuggerTierSpec],
    on_status: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    """Try to fix a post-flight validator failure by dispatching the
    AgenticDebugger across an escalation chain.

    Parameters
    ----------
    service_name:
        Service the failure belongs to (e.g. ``"backend"``). Used in
        log messages and sticky-log entries.
    workspace:
        Service workspace. The debugger will read source files from it
        and write fixes back into it.
    validator_output:
        The mypy/tsc stdout+stderr blob. Fed to the debugger as
        ``error_output``.
    failing_files:
        Files mypy/tsc named in its errors. Used as ``source_files``
        for the debugger so it focuses on the right modules.
    rerun_validator:
        Closure that re-runs the validator and returns
        ``(passed: bool, output: str)``. Architect supplies one bound
        to ``run_validator(service, workspace_root)``.
    escalation:
        Tier list — same shape integration repair uses. Each tier's
        ``factory(workspace)`` returns a debugger; ``max_turns`` and
        ``repair_attempts`` cap effort per tier.
    """
    last_output = validator_output
    ws_root = _ws_root_path(workspace)

    # Empty source_files would leave the debugger blind. Fall back to
    # discovering files from the validator output if the caller didn't
    # supply any.
    source_files = list(failing_files)
    if not source_files:
        # Best-effort: validator output usually starts each line with
        # "<rel/path>:<line>: error: …". Pull unique paths.
        seen = set()
        for line in validator_output.splitlines():
            head = line.split(":", 1)[0].strip()
            if head and head not in seen and not head.startswith(("error", "Found", "==")):
                # crude filter: looks like a file path
                if "/" in head or head.endswith((".py", ".ts", ".tsx")):
                    source_files.append(head)
                    seen.add(head)

    test_files: List[str] = []  # post-flight is type-checking, not test running

    for tier in escalation:
        tier_label = tier.model_label
        for attempt in range(1, tier.repair_attempts + 1):
            _log(
                on_status,
                f"Post-flight repair: '{service_name}' tier {tier_label} "
                f"attempt {attempt}/{tier.repair_attempts}..."
            )

            # Sticky log → debugger's repair_history. Same persistent
            # file integration debug uses, so subsequent debugger turns
            # see the full repair narrative for this service.
            sticky_block = _log_format(ws_root) if ws_root else ""
            combined_history = [sticky_block] if sticky_block else []

            try:
                debugger = tier.factory(workspace)
                if hasattr(debugger, "_max_turns"):
                    debugger._max_turns = tier.max_turns

                diagnosis = debugger.diagnose(
                    error_output=last_output,
                    source_files=source_files,
                    test_files=test_files,
                    repair_history=combined_history,
                )
            except Exception as e:
                _log(
                    on_status,
                    f"Post-flight repair: diagnose raised "
                    f"({type(e).__name__}: {e}) — giving up at tier "
                    f"'{tier_label}'"
                )
                if ws_root is not None:
                    _log_append(ws_root, _LogEntry(
                        agent="postflight",
                        tier=tier_label,
                        attempt=attempt,
                        trigger=last_output[:500],
                        diagnosis=f"diagnose raised: {type(e).__name__}: {e}",
                        outcome="error",
                    ))
                return False, last_output

            _log(
                on_status,
                f"Post-flight repair: '{service_name}' diagnosis — "
                f"{diagnosis.root_cause_category}, "
                f"{len(diagnosis.code_fixes)} direct fix(es)"
            )

            if not diagnosis.code_fixes:
                if ws_root is not None:
                    _log_append(ws_root, _LogEntry(
                        agent="postflight",
                        tier=tier_label,
                        attempt=attempt,
                        trigger=last_output[:500],
                        diagnosis=diagnosis.diagnosis[:500],
                        outcome="no_fixes",
                    ))
                continue

            applied = []
            for fix in diagnosis.code_fixes:
                try:
                    workspace.write_file(path=fix.filepath, content=fix.new_content)
                    applied.append(fix.filepath)
                    _log(on_status, f"Post-flight repair: applied fix to {fix.filepath}")
                except Exception as e:
                    _log(
                        on_status,
                        f"Post-flight repair: fix write failed for "
                        f"{fix.filepath} — {e}"
                    )

            if not applied:
                if ws_root is not None:
                    _log_append(ws_root, _LogEntry(
                        agent="postflight",
                        tier=tier_label,
                        attempt=attempt,
                        trigger=last_output[:500],
                        diagnosis=diagnosis.diagnosis[:500],
                        outcome="no_writes",
                    ))
                continue

            # Re-run the validator. If clean: we're done. If still
            # failing: log + continue to next attempt/tier with the
            # updated error context.
            try:
                passed, new_output = rerun_validator()
            except Exception as e:
                _log(
                    on_status,
                    f"Post-flight repair: rerun_validator raised — "
                    f"{type(e).__name__}: {e}"
                )
                return False, last_output

            outcome = "fixed" if passed else "still_failing"
            if ws_root is not None:
                _log_append(ws_root, _LogEntry(
                    agent="postflight",
                    tier=tier_label,
                    attempt=attempt,
                    trigger=last_output[:500],
                    diagnosis=diagnosis.diagnosis[:500],
                    fixes=[
                        {"file": f.filepath, "summary": diagnosis.diagnosis[:120]}
                        for f in diagnosis.code_fixes
                    ],
                    outcome=outcome,
                ))

            if passed:
                _log(
                    on_status,
                    f"Post-flight repair: '{service_name}' VALIDATOR NOW CLEAN "
                    f"after tier {tier_label} attempt {attempt}"
                )
                return True, new_output

            last_output = new_output
            _log(
                on_status,
                f"Post-flight repair: '{service_name}' tier {tier_label} "
                f"attempt {attempt} did not clear — continuing"
            )

    _log(
        on_status,
        f"Post-flight repair: '{service_name}' EXHAUSTED escalation chain — "
        f"validator still failing"
    )
    return False, last_output

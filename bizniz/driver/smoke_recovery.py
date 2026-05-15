"""SmokeRecovery — single-shot agent that attempts to fix a failing
smoke phase before the pipeline hard-halts.

When ``SmokePhase`` finds a critical failure (route 5xx, /health
down, /api/login broken), the pipeline currently halts at the
``smoke_failed`` gate and waits for a human. Most smoke failures
fall into a small set of cheap-to-fix patterns:

  - Stale uvicorn process (new SQLAlchemy models added in a later
    milestone never registered → tables missing). Recovery:
    ``docker compose restart <backend>``.
  - Frontend dev container holding a cached bundle after a Vite
    config change. Recovery: ``docker compose restart <frontend>``.
  - Database missing a manually-required migration. Recovery: run
    the migration inside the container.
  - Configuration drift (env var set wrong, file not synced).
    Recovery: edit the file, restart.

This agent dispatches one Claude CLI session with full Bash + file
tools, hands it the smoke failures + stack context, and gives it a
fixed-budget chance to fix things. On return, the caller re-runs
``SmokePhase``. If the second run passes, the pipeline continues
normally. If it still fails, the hard-gate fires as before — recovery
just got one shot.

Design choices:
  - Single Coder pass (no escalation chain) — keep recovery cheap.
    If it can't fix in one shot with the sticky recovery log, the
    bug isn't "state drift" — it's structural and needs human eyes.
  - Tight allowed-tools (Bash + Read + Edit + Glob + Grep + Write)
    — recovery may need to edit a config file.
  - Bounded turn budget (default 30 tool iterations) — enough to
    inspect logs + restart a container + re-curl, not enough to
    rewrite a service.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field


_DEFAULT_TIMEOUT_S = 900.0  # 15 min — generous but bounded
_ALLOWED_TOOLS = ["Bash", "Read", "Edit", "Glob", "Grep", "Write"]


class SmokeRecoveryResult(BaseModel):
    """Outcome of one recovery attempt."""
    attempted: bool = False
    succeeded: bool = False
    summary: str = ""
    actions_taken: List[str] = Field(default_factory=list)
    elapsed_s: float = 0.0
    raw_response: str = ""


class SmokeRecovery:
    """Single-shot Claude CLI agent that tries to fix smoke failures."""

    def __init__(
        self,
        compose_path: str,
        project_root: Path,
        command: str = "claude",
        timeout_seconds: float = _DEFAULT_TIMEOUT_S,
        on_status: Optional[Callable[[str], None]] = None,
        fallback_model: Optional[str] = None,
    ):
        self._compose_path = compose_path
        self._project_root = Path(project_root)
        self._command = command
        self._timeout_s = timeout_seconds
        self._on_status = on_status
        self._fallback_model = (
            fallback_model
            or os.environ.get("BIZNIZ_CLAUDE_FALLBACK_MODEL")
        )

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def recover(
        self,
        critical_failures: List[str],
        service_names: List[str],
        milestone_title: str,
    ) -> SmokeRecoveryResult:
        """Dispatch one Claude CLI session to attempt recovery.

        Returns a ``SmokeRecoveryResult``. Caller re-runs SmokePhase
        and decides whether to proceed (succeeded=True or re-run
        passes) or hard-halt (succeeded=False and re-run still
        fails).
        """
        if shutil.which(self._command) is None:
            self._log("SmokeRecovery: claude binary not on PATH; skipping")
            return SmokeRecoveryResult(
                attempted=False,
                succeeded=False,
                summary="claude binary not available",
            )

        prompt = self._build_prompt(
            critical_failures=critical_failures,
            service_names=service_names,
            milestone_title=milestone_title,
        )
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", _SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(_ALLOWED_TOOLS),
            "--add-dir", str(self._project_root),
        ]
        if self._fallback_model:
            cmd.extend(["--fallback-model", self._fallback_model])

        self._log(
            f"SmokeRecovery: dispatching for {len(critical_failures)} "
            f"failure(s); milestone='{milestone_title}'"
        )
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                cwd=str(self._project_root),
            )
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            self._log(
                f"SmokeRecovery: timed out after {elapsed:.0f}s"
            )
            return SmokeRecoveryResult(
                attempted=True,
                succeeded=False,
                summary=f"recovery timed out after {self._timeout_s:.0f}s",
                elapsed_s=elapsed,
            )
        except FileNotFoundError as e:
            return SmokeRecoveryResult(
                attempted=False,
                succeeded=False,
                summary=f"claude binary missing at runtime: {e}",
            )
        elapsed = time.time() - t0

        if proc.returncode != 0:
            self._log(
                f"SmokeRecovery: claude exited {proc.returncode}: "
                f"{(proc.stderr or '')[:200]}"
            )
            return SmokeRecoveryResult(
                attempted=True,
                succeeded=False,
                summary=f"claude exited {proc.returncode}",
                elapsed_s=elapsed,
                raw_response=(proc.stdout or "")[:2000],
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return SmokeRecoveryResult(
                attempted=True,
                succeeded=False,
                summary="non-JSON CLI output",
                elapsed_s=elapsed,
                raw_response=(proc.stdout or "")[:2000],
            )
        if payload.get("is_error"):
            return SmokeRecoveryResult(
                attempted=True,
                succeeded=False,
                summary="claude is_error=true",
                elapsed_s=elapsed,
                raw_response=(payload.get("result") or "")[:2000],
            )

        result_text = payload.get("result") or ""
        actions = self._extract_actions(result_text)
        # Model self-reports success at the end of recovery — we
        # still re-run smoke to verify externally. This flag is just
        # a quick "did anything happen" signal.
        self_reported_ok = (
            "RECOVERY SUCCESS" in result_text
            or "recovery succeeded" in result_text.lower()
        )
        self._log(
            f"SmokeRecovery: returned in {elapsed:.1f}s — "
            f"{len(actions)} action(s); self_reported_ok={self_reported_ok}"
        )
        return SmokeRecoveryResult(
            attempted=True,
            succeeded=self_reported_ok,
            summary=result_text[:400],
            actions_taken=actions,
            elapsed_s=elapsed,
            raw_response=result_text[:4000],
        )

    def _build_prompt(
        self,
        critical_failures: List[str],
        service_names: List[str],
        milestone_title: str,
    ) -> str:
        failure_block = "\n".join(f"  - {f}" for f in critical_failures)
        services_block = ", ".join(service_names) if service_names else "(unknown)"
        return _USER_TEMPLATE.format(
            milestone_title=milestone_title,
            failure_block=failure_block,
            services_block=services_block,
            compose_path=self._compose_path,
            project_root=str(self._project_root),
        )

    @staticmethod
    def _extract_actions(result_text: str) -> List[str]:
        """Pull a short list of ``ACTION:`` lines the model emits, for
        diagnostics. Best-effort — empty list if model didn't emit
        the expected format."""
        actions: List[str] = []
        for line in result_text.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("ACTION:"):
                actions.append(stripped[7:].strip()[:200])
        return actions


_SYSTEM_PROMPT = """\
You are a smoke-test recovery agent for the bizniz build pipeline.

The smoke phase makes simple HTTP probes against a running docker
compose stack (health endpoints, login flow, route GETs). It just
caught critical failures. Before the pipeline hard-halts and waits
for a human, you get one shot at recovery.

Common patterns you can fix:

  1. **Stale running process** — backend container restarted long
     ago, source files have changed since, new SQLAlchemy models
     never registered → tables missing.
     Recovery: ``docker compose -f <compose_path> restart <service>``
     and wait for the service to become healthy.
  2. **Missing migration / schema drift** — table referenced in code
     doesn't exist in the database.
     Recovery: ``docker exec <db_container> psql ... -c "CREATE
     TABLE ..."``, or trigger the app's migration via
     ``docker compose exec <backend> python -m alembic upgrade head``
     if Alembic is configured.
  3. **Configuration drift** — env var typo, .env file missing, etc.
     Recovery: edit the file, restart the affected container.
  4. **Frontend dev server cached a broken bundle** —
     Recovery: ``docker compose restart <frontend>``.

What NOT to do:
  - Don't rewrite application code. The Coder phase already did
    that work; if the bug is in shipped code, it'll surface again
    in the next milestone. Surface for human.
  - Don't run destructive migrations (``DROP TABLE``, ``DELETE``
    without WHERE). If the data is wrong, halt for human review.
  - Don't push the failure under the rug by relaxing the smoke
    check itself. The check is the contract.

Your toolset: Bash (for ``docker compose``, ``docker exec``, ``curl``,
``psql``), Read, Edit, Write, Glob, Grep. You have permissive
permissions — no human will be asked to approve individual tool
calls.

Workflow:
  1. ``docker logs --tail 50 <service>`` for each failing service to
     see what's actually breaking.
  2. ``docker exec <db_container> psql -U $USER -d <db> -c "\\dt"``
     to inspect schema state, if a relation-not-found error
     surfaces.
  3. Pick the smallest reversible action that addresses the root
     cause. Restart > recreate > rebuild > edit-and-restart.
  4. Re-probe the failing route with ``curl`` to verify the fix
     actually worked.
  5. End your final assistant message with ONE of:
       - ``ACTION: <one-line summary of what you did>``  (repeat
         per action)
       - ``RECOVERY SUCCESS: <one-paragraph summary>``  (when the
         re-probe returned 2xx)
       - ``RECOVERY FAILED: <one-paragraph reason>``  (when you
         can't fix it without human input)

The harness will independently re-run the smoke phase after your
return — RECOVERY SUCCESS is your claim, but the re-check is the
truth. So don't lie; if your fix didn't take, say RECOVERY FAILED
so the human can see the right state.
"""


_USER_TEMPLATE = """\
SMOKE FAILURE — milestone: {milestone_title}

The smoke phase just halted with these critical failures:

{failure_block}

Stack context:
  - compose file: {compose_path}
  - project root: {project_root}
  - services in this stack: {services_block}

Attempt recovery per your system prompt. Bias toward the cheapest
reversible action (container restart > config edit > rebuild).
Return the final summary lines (ACTION / RECOVERY SUCCESS /
RECOVERY FAILED) when done.
"""

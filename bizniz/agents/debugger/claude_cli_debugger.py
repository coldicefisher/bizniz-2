"""ClaudeCliDebugger — diagnose-and-fix backed by ``claude --print``.

Parallel to ``ClaudeCliCoder`` (same approach, different agent role).
Drop-in replacement for ``AgenticDebugger.diagnose``: same input
shape, same ``AgenticDiagnosis`` return type. The
``repair_integration_failure`` loop doesn't see the difference.

Why a separate class instead of routing AgenticDebugger through
``ClaudeCliClient``: the legacy debugger uses a JSON-schema action
loop (view_file → run_command → tail_logs → submit_fix). Claude
CLI doesn't honor that schema; trying to force it produces:
  - timeouts (Claude spends 10 minutes trying to match our schema)
  - parse failures (free-text responses don't fit JSON)
  - lost work (Claude already Edit'd files but we expect them in
    ``code_fixes``)

This class lets Claude be Claude: native Edit/Write/Read/Bash
tools, free-text reasoning, and a final structured-output JSON
summary that we parse for the diagnosis envelope.

Property_manager_claude M1 (2026-05-12) was the motivating run:
integration phase halted at gate because Claude+legacy-debugger
timed out twice and parse-failed three times.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.agents.debugger.types import (
    AgenticDebuggerError,
    AgenticDebuggerGaveUpError,
    AgenticDebuggerTimeoutError,
    AgenticDiagnosis,
    CodeFix,
)
from bizniz.workspace.base_workspace import BaseWorkspace


# Allowed tools Claude can use inside the debugger session. Same
# set as the Coder uses — file ops + Bash for container
# introspection, no WebFetch.
_ALLOWED_TOOLS = ["Edit", "Write", "Read", "Bash", "Glob", "Grep"]


_DEBUGGER_SYSTEM_PROMPT = """\
You are a debugging agent for a Bizniz pipeline. Integration tests
just failed against the live Docker stack and you have ~10 minutes
to diagnose and fix the problem.

# Your toolbox

- **Edit / Write / Read**: file ops on the workspace (current dir).
- **Bash**: ``docker compose -f <path> exec -T <svc> <cmd>`` to run
  things in a service container; ``docker compose logs --tail N
  <svc>`` for upstream logs; ``curl`` to hit endpoints from the
  host. The compose stack is already up.
- **Glob / Grep**: filesystem search.

# Workflow

1. **Read the error output** the dispatcher hands you. Find the
   actual failure (ImportError, AttributeError, 500 with
   traceback, schema mismatch, etc.).
2. **Probe live state** before editing files. Common moves:
   - ``tail_logs`` of the target service → see the server-side
     traceback that the test's ``assert 200 == 500`` hides.
   - ``hit_endpoint`` to repro the failing call and see the
     response body (most 4xx/5xx have the cause in the body).
   - ``pip list`` / ``cat requirements.txt`` for dep mismatches.
   - ``inspect_env`` for config drift.
3. **Apply the fix**. You may Edit files directly — they're on
   the live workspace mounted into the container. Don't
   accumulate a diff; just write the change.
4. **Verify**. Re-run the failing test via Bash.
5. **Output the diagnosis JSON** as your final message (schema
   below). The dispatcher reads your last message verbatim.

# Final output (REQUIRED)

Your LAST message must be a single JSON object — no fences, no
prose:

```
{
  "diagnosis": "one-paragraph root cause",
  "root_cause_category": "import_error | logic_error | dependency_issue | config_issue | test_issue | other",
  "fix_target": "code" | "tests" | "both",
  "affected_files": ["path/relative/to/workspace"],
  "fix_plan": ["step 1", "step 2"],
  "suggested_approach": "one-line summary of what you did",
  "missing_packages": [],
  "confidence": "high" | "medium" | "low",
  "code_fixes": []
}
```

``code_fixes`` is EMPTY because you've already applied the fix via
Edit/Write. The dispatcher will re-run the tests against the live
workspace to verify.

If you genuinely cannot find the bug, output the diagnosis
anyway with ``confidence: "low"`` and an honest
``suggested_approach``. The dispatcher needs a structured answer
either way.
"""


class ClaudeCliDebugger:
    """Drop-in replacement for AgenticDebugger backed by Claude CLI.

    Mirrors AgenticDebugger's public ``diagnose`` method signature.
    Construction surface accepts the same kwargs so v2_build's
    ``debugger_factory`` can swap on a model-name flag.
    """

    def __init__(
        self,
        client=None,  # ignored; CLI handles its own auth
        workspace: Optional[BaseWorkspace] = None,
        environment=None,  # ignored
        tool_iterations: int = 15,  # ignored; Claude manages its own loop
        timeout_seconds: int = 600,
        on_status_message: Optional[Callable[[str], None]] = None,
        compose_path: Optional[str] = None,
        service_name: Optional[str] = None,
        command: str = "claude",
        additional_args: Optional[List[str]] = None,
        model_name: str = "claude-cli",
    ):
        self._workspace = workspace
        self._compose_path = compose_path or ""
        self._service_name = service_name or ""
        self._timeout_s = float(timeout_seconds)
        self._on_status = on_status_message
        self._command = command
        self._additional_args = list(additional_args or [])
        self._model_name = model_name

        if shutil.which(self._command) is None:
            raise AgenticDebuggerError(
                f"ClaudeCliDebugger: ``{self._command}`` not on PATH. "
                f"Install Claude Code or set ``backends.claude_cli."
                f"command`` in bizniz.yaml."
            )

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    # ── BaseDebugger contract ──────────────────────────────────────────

    @property
    def _ai_client(self):
        """Stub — exists because BaseDebugger.diagnose may inspect it
        for cost-tracking. We don't have a long-lived client object."""
        return None

    # ── Public ─────────────────────────────────────────────────────────

    def diagnose(
        self,
        error_output: str,
        source_files: Dict[str, str],
        test_files: Dict[str, str],
        architecture_context: str = "",
        repair_history: Optional[List[str]] = None,
    ) -> AgenticDiagnosis:
        """Run Claude once to diagnose + fix. Returns an
        AgenticDiagnosis parsed from Claude's final JSON output.

        ``source_files`` and ``test_files`` are passed for context
        but Claude reads from the live workspace via ``Read`` —
        the dicts are reproducing what's already on disk.

        Fix application: Claude uses ``Edit``/``Write`` directly
        against the workspace; ``code_fixes`` in the return is
        typically empty because the work is already on disk. The
        dispatcher's rerun-and-verify loop picks up the live
        state.
        """
        repair_history = repair_history or []
        self._log("ClaudeCliDebugger: starting diagnosis...")

        prompt = self._build_user_prompt(
            error_output=error_output,
            source_files=source_files,
            test_files=test_files,
            architecture_context=architecture_context,
            repair_history=repair_history,
        )

        ws_root = self._resolve_workspace_root()
        mcp_config_path = self._write_mcp_config()
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", _DEBUGGER_SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(_ALLOWED_TOOLS),
            "--add-dir", str(ws_root),
            "--mcp-config", str(mcp_config_path),
        ] + self._additional_args

        env = os.environ.copy()
        env["BIZNIZ_PROJECT_ROOT"] = str(self._project_root_for_mcp())
        env["BIZNIZ_JOB_ID"] = self._infer_job_id() or ""

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                cwd=str(ws_root),
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - t0
            self._log(
                f"ClaudeCliDebugger: timeout after {int(elapsed)}s — "
                f"returning low-confidence diagnosis"
            )
            raise AgenticDebuggerTimeoutError(
                f"claude --print debugger timed out after "
                f"{self._timeout_s:.0f}s"
            ) from e
        except FileNotFoundError as e:
            raise AgenticDebuggerError(
                f"claude binary disappeared between init and run: {e}"
            ) from e

        elapsed = time.time() - t0
        self._log(
            f"ClaudeCliDebugger: subprocess done in {elapsed:.1f}s "
            f"(exit {proc.returncode})"
        )

        if proc.returncode != 0:
            raise AgenticDebuggerError(
                f"claude --print exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[:400]}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise AgenticDebuggerError(
                f"claude --print returned non-JSON: {e}\n"
                f"stdout head: {proc.stdout[:400]}"
            ) from e

        if payload.get("is_error"):
            raise AgenticDebuggerError(
                f"claude --print is_error=true: "
                f"{payload.get('result', '')[:400]}"
            )

        result_text = payload.get("result") or ""
        self._maybe_record_cost(payload, elapsed)
        return self._parse_diagnosis(result_text)

    # ── Internals ──────────────────────────────────────────────────────

    def _resolve_workspace_root(self) -> Path:
        if self._workspace is not None and hasattr(self._workspace, "root"):
            return Path(self._workspace.root)
        return Path(".")

    def _project_root_for_mcp(self) -> Path:
        return self._resolve_workspace_root().parent

    def _infer_job_id(self) -> Optional[str]:
        runs = self._project_root_for_mcp() / "docs" / "runs"
        if not runs.exists():
            return None
        dirs = sorted(
            [p.name for p in runs.iterdir() if p.is_dir()],
            reverse=True,
        )
        return dirs[0] if dirs else None

    def _write_mcp_config(self) -> Path:
        """Same MCP config the Coder writes — gives the debugger
        access to prior-issue context + audit findings + import
        validator. Tempfile per invocation."""
        bizniz_root = Path(__file__).resolve().parents[3]
        config = {
            "mcpServers": {
                "bizniz": {
                    "command": sys.executable,
                    "args": ["-m", "bizniz.mcp_server.server"],
                    "env": {
                        "PYTHONPATH": str(bizniz_root),
                        "BIZNIZ_PROJECT_ROOT": str(self._project_root_for_mcp()),
                        "BIZNIZ_JOB_ID": self._infer_job_id() or "",
                    },
                },
            },
        }
        fd, path = tempfile.mkstemp(prefix="bizniz_dbg_mcp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)
        return Path(path)

    def _build_user_prompt(
        self,
        error_output: str,
        source_files: Dict[str, str],
        test_files: Dict[str, str],
        architecture_context: str,
        repair_history: List[str],
    ) -> str:
        """Compose the debug context. Files are listed by path only —
        Claude can ``Read`` them directly from the workspace; we
        don't need to inline content (token-cheap)."""
        parts: List[str] = []
        parts.append("# Integration test failure — diagnose and fix\n")

        parts.append("\n## Failure output (pytest)\n```\n")
        parts.append(error_output[:6000])
        parts.append("\n```\n")

        if architecture_context:
            parts.append(
                f"\n## Architecture context\n"
                f"{architecture_context[:3000]}\n"
            )

        ws_root = self._resolve_workspace_root()
        parts.append(
            f"\n## Workspace\n"
            f"- Working directory: ``{ws_root}``\n"
            f"- Service container: ``{self._service_name}``\n"
            f"- Compose path: ``{self._compose_path}``\n"
        )

        if source_files:
            parts.append("\n## Source files in scope (Read these if relevant)\n")
            for path in sorted(source_files.keys()):
                parts.append(f"  - `{path}`\n")
        if test_files:
            parts.append("\n## Test files in scope\n")
            for path in sorted(test_files.keys()):
                parts.append(f"  - `{path}`\n")

        if repair_history:
            parts.append("\n## Prior repair attempts (this same run)\n")
            for i, entry in enumerate(repair_history[-5:], 1):
                parts.append(f"  {i}. {entry[:300]}\n")
            parts.append(
                "\nDon't repeat what didn't work last time — escalate the "
                "approach.\n"
            )

        parts.append(
            f"\n## Your job\n"
            f"1. Read the error output.\n"
            f"2. Probe live state (tail_logs, hit_endpoint, pip list, "
            f"inspect_env) — DO NOT guess from source alone.\n"
            f"3. Edit files directly to apply the fix.\n"
            f"4. Re-run the failing test via Bash to confirm.\n"
            f"5. Emit the diagnosis JSON as your final message.\n"
            f"\nIf the bug needs cross-issue context (what another "
            f"issue's tests covered, what the CR flagged), call the "
            f"``mcp__bizniz__*`` tools — they expose the project DB.\n"
        )
        return "".join(parts)

    @staticmethod
    def _extract_json_object(text: str) -> Optional[str]:
        """Same lenient extractor the Coder uses. Bare JSON, fenced
        JSON, or trailing JSON after prose."""
        if not text:
            return None
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped

        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            return fence.group(1).strip()

        end = text.rfind("}")
        if end == -1:
            return None
        depth = 0
        for i in range(end, -1, -1):
            if text[i] == "}":
                depth += 1
            elif text[i] == "{":
                depth -= 1
                if depth == 0:
                    return text[i:end + 1]
        return None

    def _parse_diagnosis(self, text: str) -> AgenticDiagnosis:
        """Coerce Claude's final JSON into an AgenticDiagnosis.
        Lenient — fields default to empty if missing; we never
        crash on shape drift."""
        candidate = self._extract_json_object(text)
        if not candidate:
            self._log(
                "ClaudeCliDebugger: final output had no JSON object — "
                "returning low-confidence stub"
            )
            return AgenticDiagnosis(
                diagnosis=(
                    "Claude did not emit a final diagnosis JSON. Last "
                    "message text follows."
                ),
                root_cause_category="other",
                fix_target="code",
                fix_plan=[],
                suggested_approach=text[:500],
                confidence="low",
                code_fixes=[],
            )
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            self._log(
                "ClaudeCliDebugger: final JSON was malformed — "
                "returning low-confidence stub"
            )
            return AgenticDiagnosis(
                diagnosis="malformed final JSON",
                root_cause_category="other",
                fix_target="code",
                suggested_approach=candidate[:500],
                confidence="low",
            )

        # Defensive coercion — Claude may emit extra fields or
        # miss optional ones. We accept any subset and fill in
        # defaults.
        fix_target = data.get("fix_target") or "code"
        if fix_target not in ("code", "tests", "both"):
            fix_target = "code"
        confidence = data.get("confidence") or "medium"
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"

        raw_fixes = data.get("code_fixes") or []
        code_fixes: List[CodeFix] = []
        for f in raw_fixes:
            if isinstance(f, dict) and "filepath" in f and "new_content" in f:
                code_fixes.append(CodeFix(
                    filepath=str(f["filepath"]),
                    new_content=str(f["new_content"]),
                ))

        return AgenticDiagnosis(
            diagnosis=str(data.get("diagnosis") or ""),
            root_cause_category=str(data.get("root_cause_category") or "other"),
            fix_target=fix_target,
            affected_files=[str(p) for p in (data.get("affected_files") or [])],
            fix_plan=[str(s) for s in (data.get("fix_plan") or [])],
            suggested_approach=str(data.get("suggested_approach") or ""),
            missing_packages=[
                str(p) for p in (data.get("missing_packages") or [])
            ],
            confidence=confidence,
            code_fixes=code_fixes,
        )

    def _maybe_record_cost(self, payload: dict, elapsed: float) -> None:
        try:
            from bizniz.cost import get_tracker
            tracker = get_tracker()
            if tracker is None:
                return
            usage = payload.get("usage") or {}
            tracker.record(
                agent="agenticdebugger",
                model=self._model_name,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                duration_ms=int(elapsed * 1000),
                cached_input_tokens=(
                    int(usage.get("cache_read_input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0)
                ),
            )
        except Exception:
            pass

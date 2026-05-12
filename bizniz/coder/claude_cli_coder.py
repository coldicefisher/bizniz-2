"""ClaudeCliCoder — Coder.code_issue() backed by ``claude --print``.

Drop-in replacement for ``Coder``: same constructor surface, same
``code_issue`` contract, same ``CoderResult`` return type. The
orchestrator + dispatcher don't know which one ran.

Design: let Claude be Claude. We don't impose our JSON-schema
action loop — Claude uses its native ``Edit``, ``Write``, ``Read``,
``Bash``, ``Glob``, ``Grep`` tools. The subprocess runs ``claude
--print --output-format=json`` from the service's workspace
directory, with permissions bypassed so the model can run
``docker compose exec`` for tests without prompting.

The contract back to Bizniz: Claude's FINAL output (the JSON
``result`` field from ``--output-format=json``) must be a single
JSON object matching ``CoderResult``. The system prompt makes this
explicit. We parse the JSON, build the typed result, and return.

Why not reuse the JSON-schema action loop: Claude is great at
tool use; trying to force its responses into our schema is
fighting the model. The same prompt the v2.5 Coder uses for Gemini
becomes a high-level brief here.
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
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from bizniz.architect.types import SystemArchitecture
from bizniz.coder.prompts.initial_context import build_coder_initial_context
from bizniz.coder.prompts.system_prompt import CODER_SYSTEM_PROMPT
from bizniz.coder.types import CoderError, CoderResult, Issue
from bizniz.quality_engineer.types import EnrichedSpec
from bizniz.workspace.base_workspace import BaseWorkspace


# Final-output instruction appended to the system prompt. Tells Claude
# its LAST action must be to emit a single JSON object matching
# CoderResult — no fences, no prose. We parse stdout for it.
_FINAL_OUTPUT_INSTRUCTION = """\

# FINAL OUTPUT (REQUIRED)

When you're done implementing the issue, your VERY LAST message must
be a single JSON object — no markdown fences, no prose, nothing else.
The Bizniz dispatcher parses your last message verbatim.

Schema:
```
{
  "issue_id": "<the issue id from the prompt>",
  "status": "passed" | "partial" | "failed" | "deferred",
  "target_files_written": ["path/relative/to/workspace.py", ...],
  "test_files_written":   ["path/relative/to/workspace.py", ...],
  "summary": "one-sentence description of what you did",
  "notes": ["any caveats or follow-ups the dispatcher should know"]
}
```

Use ``status="passed"`` only when you ran the issue's tests and they
ALL passed (pytest exit 0). Otherwise ``"partial"`` if some tests
fail, ``"deferred"`` if the issue is blocked outside your scope, or
``"failed"`` if you can't make progress at all.
"""


# Tools Claude is allowed to use. Edit/Write/Read/Bash cover the
# main work; Glob/Grep cover discovery. WebFetch/WebSearch and the
# specialist agents are off — this Coder runs offline against the
# repo.
_ALLOWED_TOOLS = ["Edit", "Write", "Read", "Bash", "Glob", "Grep"]


class ClaudeCliCoder:
    """Tool-loop coder backed by the Claude Code CLI subprocess.

    Construction surface matches ``bizniz.coder.agent.Coder`` so
    ``examples.v2_build.coder_factory`` can swap based on a
    config flag without touching the orchestrator.

    ``client`` is accepted but ignored — the CLI handles its own
    model selection from the user's logged-in session. Pass
    ``model_name`` if you want Bizniz cost tags to record which
    backend was used.
    """

    def __init__(
        self,
        client=None,
        workspace: Optional[BaseWorkspace] = None,
        compose_path: str = "",
        target_service: str = "",
        on_status: Optional[Callable[[str], None]] = None,
        tool_iterations: int = 30,  # unused; Claude manages its own loop
        timeout_seconds: int = 1200,
        base_url: Optional[str] = None,
        workspace_name: Optional[str] = None,
        runner: str = "pytest",  # informational; Claude reads from prompt
        model_name: str = "claude-cli",
        command: str = "claude",
        additional_args: Optional[List[str]] = None,
    ):
        self._workspace = workspace
        self._compose_path = compose_path
        self._target_service = target_service
        self._on_status = on_status
        self._timeout_s = float(timeout_seconds)
        self._base_url = base_url
        self._workspace_name = workspace_name or target_service
        self._runner = runner
        self._model_name = model_name
        self._command = command
        self._additional_args = list(additional_args or [])

        if shutil.which(self._command) is None:
            raise CoderError(
                f"ClaudeCliCoder: ``{self._command}`` not on PATH. "
                f"Install Claude Code or set ``backends.claude_cli."
                f"command`` in bizniz.yaml."
            )

    def _log(self, msg: str) -> None:
        if self._on_status:
            self._on_status(msg)

    # ── Public ──────────────────────────────────────────────────────────

    def code_issue(
        self,
        issue: Issue,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        auth_contract: Optional[str] = None,
        workspace_summary: Optional[str] = None,
        skeleton_md: Optional[str] = None,
    ) -> CoderResult:
        """Run Claude once for the issue. Returns a CoderResult parsed
        from Claude's final JSON output.

        Raises ``CoderError`` on subprocess failure (binary missing,
        timeout, non-zero exit, unparseable output).
        """
        self._log(
            f"ClaudeCliCoder: {issue.id} — {issue.title} "
            f"(service={issue.service})"
        )

        # Compose system + user content. The system content is the
        # same prompt the Gemini Coder uses (workflow, hard
        # constraints, etc.) plus the final-output JSON instruction.
        system_prompt = CODER_SYSTEM_PROMPT + _FINAL_OUTPUT_INSTRUCTION
        user_prompt = self._build_user_prompt(
            issue, architecture, enriched_spec,
            auth_contract, workspace_summary, skeleton_md,
        )

        ws_root = self._resolve_workspace_root()

        # MCP config exposes bizniz tools (get_prior_issues,
        # validate_python_imports, read_audit_findings, etc.) to
        # Claude on demand. Written to a tempfile because Claude
        # CLI's --mcp-config takes a path, not inline JSON.
        mcp_config_path = self._write_mcp_config()

        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", system_prompt,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(_ALLOWED_TOOLS),
            "--add-dir", str(ws_root),
            "--mcp-config", str(mcp_config_path),
        ] + self._additional_args

        # Pass project context to the MCP server via env so its tools
        # can find the DB + run state without parsing args.
        env = os.environ.copy()
        env["BIZNIZ_PROJECT_ROOT"] = str(self._project_root_for_mcp())
        env["BIZNIZ_JOB_ID"] = self._infer_job_id() or ""

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                input=user_prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                cwd=str(ws_root),
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            self._log(
                f"ClaudeCliCoder: {issue.id} timed out after "
                f"{self._timeout_s:.0f}s"
            )
            raise CoderError(
                f"claude --print timed out after {self._timeout_s:.0f}s"
            ) from e
        except FileNotFoundError as e:
            raise CoderError(
                f"claude binary disappeared between init and run: {e}"
            ) from e

        elapsed = time.time() - t0
        self._log(
            f"ClaudeCliCoder: {issue.id} subprocess done in "
            f"{elapsed:.1f}s (exit {proc.returncode})"
        )

        if proc.returncode != 0:
            raise CoderError(
                f"claude --print exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[:400]}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise CoderError(
                f"claude --print returned non-JSON: {e}\n"
                f"stdout head: {proc.stdout[:400]}"
            ) from e

        if payload.get("is_error"):
            raise CoderError(
                f"claude --print is_error=true: "
                f"{payload.get('result', '')[:400]}"
            )

        result_text = payload.get("result") or ""
        session_id = payload.get("session_id") or str(uuid.uuid4())

        # Record cost (best-effort, off the same path the single-call
        # client uses).
        self._maybe_record_cost(payload, session_id, elapsed)

        # Parse Claude's final JSON. Be lenient — extract from prose
        # or fences if the model didn't follow the instruction
        # cleanly.
        coder_result = self._parse_coder_result(result_text, issue.id)
        return coder_result

    # ── Internals ──────────────────────────────────────────────────────

    def _resolve_workspace_root(self) -> Path:
        if self._workspace is not None and hasattr(self._workspace, "root"):
            return Path(self._workspace.root)
        return Path(".")

    def _project_root_for_mcp(self) -> Path:
        """Project root = workspace's parent (each service workspace
        lives at ``<project_root>/<service_name>/``). The MCP server
        needs project_root to find ``.bizniz/project.db`` and
        ``AUTH_CONTRACT.md``.
        """
        ws = self._resolve_workspace_root()
        return ws.parent

    def _infer_job_id(self) -> Optional[str]:
        """Best-effort: find the newest job dir under
        ``docs/runs/`` so the MCP server can locate the right
        review artifact. Returns None if no runs exist yet (first
        invocation in a fresh project).
        """
        runs = self._project_root_for_mcp() / "docs" / "runs"
        if not runs.exists():
            return None
        dirs = sorted(
            [p.name for p in runs.iterdir() if p.is_dir()],
            reverse=True,
        )
        return dirs[0] if dirs else None

    def _write_mcp_config(self) -> Path:
        """Write a tempfile MCP config that Claude CLI loads via
        ``--mcp-config``. Launches ``bizniz.mcp_server`` as a
        stdio subprocess with the project's PYTHONPATH set.

        The config is rewritten per ``code_issue`` call (cheap) so
        environment changes between issues are honored.
        """
        # The bizniz repo root — needed on PYTHONPATH for the server
        # subprocess to import bizniz packages.
        bizniz_root = Path(__file__).resolve().parent.parent.parent
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
        fd, path = tempfile.mkstemp(prefix="bizniz_mcp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)
        return Path(path)

    def _build_user_prompt(
        self,
        issue: Issue,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        auth_contract: Optional[str],
        workspace_summary: Optional[str],
        skeleton_md: Optional[str],
    ) -> str:
        """Reuse the v2.5 Coder's initial-context builder, then bolt
        on Claude-specific orientation (workspace root, compose path,
        test command convention).
        """
        base = build_coder_initial_context(
            issue=issue,
            architecture=architecture,
            enriched_spec=enriched_spec,
            auth_contract=auth_contract,
            workspace_summary=workspace_summary,
            skeleton_md=skeleton_md,
        )
        ws_root = self._resolve_workspace_root()
        runner_cmd = self._format_runner_command()
        orientation = (
            "\n\n## Environment\n"
            f"- Your working directory is: ``{ws_root}`` — files you "
            f"Edit/Write/Read are relative to this.\n"
            f"- The compose stack is up. Service container name: "
            f"``{self._target_service}``.\n"
            f"- Run tests with Bash: ``{runner_cmd}``\n"
            f"- For container introspection: ``docker compose -f "
            f"{self._compose_path} exec -T {self._target_service} "
            f"<cmd>`` (e.g. pip list, env, python -c).\n"
            f"- For upstream service logs: ``docker compose -f "
            f"{self._compose_path} logs --tail 50 <svc>``\n"
            f"\n## Bizniz MCP tools (call when you need cross-issue "
            f"context)\n"
            f"- ``mcp__bizniz__get_prior_issues(milestone, service)`` "
            f"— see what other issues in this milestone already did "
            f"(target_files, test_files, status). Use this when "
            f"you're about to write something that another issue may "
            f"have covered.\n"
            f"- ``mcp__bizniz__get_issue_test_output(issue_id)`` — "
            f"pytest output tail for another issue. Useful when "
            f"debugging a test that depends on another issue's setup.\n"
            f"- ``mcp__bizniz__validate_python_imports(file_paths)`` "
            f"— AST-walk validator. Catches hallucinated imports + "
            f"attribute access on Pydantic/dataclass classes "
            f"(``settings.foo_bar`` when only ``foo_baz`` exists). "
            f"CHEAP — call this on your target files before running "
            f"pytest to catch import-time bugs without a full test run.\n"
            f"- ``mcp__bizniz__read_audit_findings(milestone)`` — "
            f"prior CodeReviewer findings. Avoid re-introducing "
            f"patterns the reviewer already flagged.\n"
            f"- ``mcp__bizniz__read_auth_contract()`` — full "
            f"AUTH_CONTRACT.md. The Bizniz prompt already includes "
            f"it; this is a re-fetch path if you've drifted from "
            f"your context.\n"
        )
        return base + orientation

    def _format_runner_command(self) -> str:
        """Match the exec-into-service pattern from
        ``bizniz/lib/tools/test_runner.py`` so Claude's Bash calls
        run tests in the same place the Coder's run_tests would have.
        """
        if self._runner == "pytest":
            inner = "pytest <test_paths> -v --tb=short --no-header"
        elif self._runner in ("jest", "npm-test"):
            inner = "npm test --silent -- <test_paths>"
        elif self._runner == "vitest":
            inner = "npx vitest run <test_paths>"
        else:
            inner = f"{self._runner} <test_paths>"
        return (
            f"docker compose -f {self._compose_path} exec -T "
            f"{self._target_service} sh -c \"{inner}\""
        )

    def _parse_coder_result(self, text: str, expected_issue_id: str) -> CoderResult:
        """Extract the trailing CoderResult JSON object from Claude's
        final message. Lenient: handles raw JSON, fenced JSON, or
        JSON-after-prose. Falls back to ``partial`` if no JSON found.
        """
        candidate = self._extract_json_object(text)
        if not candidate:
            self._log(
                f"ClaudeCliCoder: no JSON in final output — marking "
                f"partial with raw text as summary"
            )
            return CoderResult(
                issue_id=expected_issue_id,
                status="partial",
                summary=(
                    "ClaudeCliCoder: model did not emit a CoderResult "
                    "JSON in its final message. Raw text follows."
                ),
                notes=[text[:500]] if text else [],
            )
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            return CoderResult(
                issue_id=expected_issue_id,
                status="partial",
                summary="ClaudeCliCoder: trailing JSON was malformed",
                notes=[candidate[:500]],
            )
        # Coerce shape — defensive against missing/extra keys.
        return CoderResult(
            issue_id=str(data.get("issue_id") or expected_issue_id),
            status=str(data.get("status") or "partial"),  # type: ignore[arg-type]
            target_files_written=list(data.get("target_files_written") or []),
            test_files_written=list(data.get("test_files_written") or []),
            summary=str(data.get("summary") or ""),
            notes=list(data.get("notes") or []),
            last_test_output_tail="",
        )

    @staticmethod
    def _extract_json_object(text: str) -> Optional[str]:
        """Pull the trailing top-level JSON object from ``text``.

        Strategies tried in order:
          1. The whole string is a JSON object → return as-is.
          2. There's a fenced ``json`` block → return its contents.
          3. There's a ``{`` ... ``}`` substring → scan from the end
             for the last balanced object.
        Returns the JSON string (caller json.loads-es it) or None.
        """
        if not text:
            return None
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped

        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            return fence.group(1).strip()

        # Last-resort balanced-brace scan from the end.
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

    def _maybe_record_cost(self, payload: dict, session_id: str, elapsed: float) -> None:
        """Best-effort cost tracking. Same caveat as ClaudeCliClient:
        Max plan absorbs the actual cost; tracker shows API-rate
        equivalent."""
        try:
            from bizniz.cost import get_tracker
            tracker = get_tracker()
            if tracker is None:
                return
            usage = payload.get("usage") or {}
            tracker.record(
                agent=f"coder:{self._target_service}",
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

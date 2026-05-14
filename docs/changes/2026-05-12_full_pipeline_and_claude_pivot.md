# Full milestone loop wired + pivot to Claude proven

**Sessions:** 2026-05-07 → 2026-05-12

**State at end:** The MilestoneLoop is complete (ENRICH → IMPLEMENT →
SMOKE → REVIEW → REPAIR → REVIEW → INTEGRATION → DONE) and runs
end-to-end on both Gemini and Claude. The Claude CLI subprocess
backend (ClaudeCliClient + ClaudeCliCoder) ships and was validated
on a clean greenfield. Gemini built bookshelf M1 to "almost passed"
($1.49, halted at CR gate); Claude built the same project to
"approved" ($0 marginal, no repair iterations, no escalations).

## The arc

| Phase | What landed |
|---|---|
| 2026-05-07 v33 backend | 14/14 green across 6 retry rounds. Five compounding fixes: compose-exec test_runner (commit e9cc7e9), auth contract extension + per-service copy + canonical callouts, auto-tail container logs on test failure, dispatcher deferred-as-satisfied for deps, unknown/empty action stall detection (commit 7761b8f). Forced-final terminal-rejected converts to stall instead of errored (commit 07ae439). |
| 2026-05-07 v33 frontend | 12/12 green. Runner switch: pytest / vitest / npm-test by service language (commits e9788cb, 9f5b18d). Parse-fail classified as transient (commit edbf545). |
| 2026-05-10/11 v33 smoke | Stack stood up, FA state drifted over 4 days. Surfaced AuthOperator bugs: `requireAuthentication=true` default blocked SPA login, smoke_login bypassed via API key so manifest's `login_verified=true` was misleading. Fixes shipped (commits 0196147, 1002d3d). |
| 2026-05-11 Coder hardening | `validate_symbols` AST-walks attribute access on known classes — catches `settings.foo_bar` when only `foo_baz` exists. Caught two real v33 bugs the pipeline shipped. Plus "never swallow exceptions" prompt rule (commits f4fa9c5, 41507fc). |
| 2026-05-11 Pipeline loop | The remaining sub-phases all wired into MilestoneLoop. SmokePhase (deterministic curl gate — health + public-flow login + route registration probes), commit 7761b8f via separate file. QualityEngineer + CodeReviewer already wired but never exercised in v2.5; fired against v33 and surfaced 4 critical real bugs (commit c346676). IntegrationPhase + HTTPApiTester + AgenticDebugger wiring: ctor missing `environment`, workspace method name `write_text` vs `write_file` (commit 180806b). |
| 2026-05-11 bookshelf-Gemini | First end-to-end pipeline run. Plan → Architect → Provision → Auth → Engineer (6 issues + 3 repair-fix) → Smoke → Review → 2× Repair → milestone_unapproved gate halted on real CR findings (hardcoded app_id, missing 409 mapping). ~50min, $1.49. Proved the loop self-heals and gates honestly. |
| 2026-05-11 Pluggable backend pivot | M2-on-Gemini dropped from plan. Gemini-flash quality ceiling proven (0/21 diagnostic-tool calls despite explicit prompt rules; "verified manually" lies; flash-top can't fix CR's findings in 2 repair iters). The new signal is Claude vs Gemini on the SAME pipeline; only the pluggable backend produces that. |
| 2026-05-11 ClaudeCliClient | Single-call seam. Subprocess to `claude --print --output-format=json --append-system-prompt=<sys>`. Routes via model prefix `claude-cli*`. Smoke-tested live: `'2+2?' → '4'`, JSON_SCHEMA mode returns clean schema-conformant JSON. 16 unit tests. |
| 2026-05-12 ClaudeCliCoder | Tool-loop seam. `claude --print` with `--permission-mode=bypassPermissions --allowed-tools "Edit Write Read Bash Glob Grep" --add-dir=<workspace>`. Claude uses native tools; final output is a CoderResult JSON we parse. Same constructor surface as Coder — `coder_factory` swaps based on model name. 14 unit tests. |
| 2026-05-12 bookshelf-Claude | Same problem statement, all agents on `claude-cli`. Engineer **8/8 first-try**, no escalations, no stalls, no rejected terminals. SmokePhase 4/4 ok. QE approved 5/5 capabilities first try. CR approved with 2 findings, 0 critical. **End-to-end milestone passed** in ~40min, $0 marginal. |

## The Gemini → Claude delta on the SAME problem

| Metric | bookshelf (Gemini) | bookshelf_claude (Claude) |
|---|---|---|
| Engineer plan | 6 issues | 8 issues, 5 dependency layers |
| Initial pass | 5 escalated, 1 deferred | **8 passed first try** |
| Repair iterations needed | 2 (didn't converge) | 0 |
| QE pass | 0/3 → 2/3 → 3/3 (3 rounds) | **5/5 first try** |
| CR | 4 critical, rejected | **0 critical, APPROVED** |
| Final state | halted at milestone_unapproved gate | DONE |
| Wall time | ~50 min | ~40 min |
| Cost | $1.49 (real) | $0 marginal (Max plan), API-equivalent ~$5-10 |

The pipeline didn't change. The orchestrator didn't change. The
agents didn't change. Just the LLM driver. The same code that
ground for 9 issues on Gemini one-shotted 8/8 on Claude.

## What's still pending

- **`bizniz_mcp` server** (Step 6 of pluggable plan). MCP tools expose
  cross-issue context to Claude on demand (`get_prior_issues`,
  `get_test_output`, `validate_python_imports`, `read_audit_findings`,
  etc.). bookshelf didn't need it — Claude one-shotted from a single
  issue's context. Becomes valuable on harder projects where
  cross-issue knowledge matters (property-manager scale).
- **Container rebuild on manifest edits** (#72). Coder edits
  `requirements.txt`, container doesn't see new deps. Currently
  Coder hot-installs via `pip install` inside the container as a
  workaround.

## What NOT to do (decisions documented)

- Don't pay for the Anthropic API when Claude Code CLI on Max plan
  is free.
- Don't run M2 on Gemini just to follow the original plan literally —
  the M2 signal would re-confirm what M1 already showed.
- Don't force Claude into our JSON-schema action loop. Let it use
  native Edit/Write/Bash/Read tools; we parse a final CoderResult
  JSON from its last message.
- Don't store mixed `Message` + dict entries in client history —
  caused a second-call crash. Always dicts.

## Next session

1. Build `bizniz_mcp` so Claude can pull cross-issue context.
2. Re-fire a harder greenfield (property_manager scale) on Claude to
   stress-test the seam under load that needs MCP.
3. Then evolve mode (M2 of bookshelf_claude or a fresh M2 trial) on
   Claude.

## Key commits this session arc

- `e9cc7e9` run_tests: exec into service container
- `7761b8f` Probe-first failure context + auth contract + dispatcher dep fix
- `07ae439` tool_loop: catch TerminalActionRejected in forced-final
- `e9788cb` Coder runner switch (pytest / npm-test / vitest)
- `9f5b18d` runner: default ts/js to vitest
- `edbf545` orchestrator: parse-fail classified as transient
- `0196147` AuthOperator: requireAuthentication=false
- `1002d3d` AuthOperator: smoke_login via public flow
- `f4fa9c5` Coder: AST attribute access validation
- `41507fc` Coder prompt: don't swallow exceptions
- `c346676` issue_store: prefix workspace_name when assembling
- `180806b` IntegrationPhase wiring (env + write_file)
- `a1fd43d` docs: drop M2-on-Gemini from pluggable plan
- `(ClaudeCliClient commit)` ClaudeCliClient: subprocess BaseAIClient
- `(ClaudeCliCoder commit)` ClaudeCliCoder: tool-loop via `claude --print`
- `370921b` ClaudeCliClient: history-shape regression + config-router patch

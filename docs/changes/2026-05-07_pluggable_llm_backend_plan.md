# Pluggable LLM backend — strategic direction

**Status:** approved 2026-05-07. Build AFTER first clean M1 + M2 ship on Gemini.

## Goal

Architecture C (per-issue narrow context + MCP for cross-issue knowledge)
with the LLM driver pluggable. Same workspace artifacts regardless of
driver. Run Gemini API and Claude CLI side-by-side. Mix per service.

User's framing:
> "Replicate myself. Ship products on command. I want a stock trading
> platform. I'd like to stand it up and build out the platform just by
> specifying what I want, in languages I understand."

Max plan covers Claude Code CLI usage at $0 marginal cost. Gemini API
stays as cheap fallback / parallelism option.

## What survives the LLM swap (the value)

- The pipeline shape: code → validate → test → document → audit → gate
- `auth_operator/` — deterministic FA setup
- `provisioner/templates/` — Postgres, Redis, FusionAuth, FastAPI/React
- `coder/symbol_validator.py` — AST import resolver
- Sidecar Docker images (bizniz-test-pytest, bizniz-test-playwright)
- Contract test renderer
- DB persistence (issue_runs, audit findings, costs)
- Pattern of "service planner → coder per issue → tester → debugger"

## What gets a swappable seam

**Single-call agents** (Planner, Architect, AuthPlanner, Enrich,
ServicePlanner, code_examples): already use ``BaseAIClient``. Add
``ClaudeCliClient(BaseAIClient)`` that subprocess-shells out to
``claude --print --output-format=json``. Returns the same
``(text, job_id, output_messages)`` tuple. Config selects which client.

**Tool-loop agents** (Coder, AgenticDebugger): replace the whole
``Coder`` class with ``ClaudeCliCoder`` that spawns one ``claude --print``
per issue. Claude uses native tools (Edit, Write, Bash) plus MCP-exposed
bizniz tools. When done, returns ``CoderResult`` (the same Pydantic type
bizniz uses today). DB and orchestrator don't care which Coder ran.

## MCP server (for cross-issue context — option C)

`bizniz_mcp` exposes deterministic tools + DB queries to whichever LLM
driver is in use:

- `bizniz.get_prior_issues(milestone)` — list all done/failed issues + dispositions
- `bizniz.get_test_output(issue_id)` — last test result from another issue
- `bizniz.read_workspace_summary()` — what's currently on disk
- `bizniz.read_audit_findings()` — what the reviewer said
- `bizniz.fusionauth_setup(spec)` — wraps FusionAuthOperator
- `bizniz.provision_project(arch)` — wraps Provisioner
- `bizniz.render_auth_contract(manifest)` — pure render
- `bizniz.validate_python_imports(path)` — wraps symbol_validator

The Coder defaults to its narrow context. When stuck, calls
`bizniz.get_prior_issues` to see cross-issue work. Best of both:
hallucination firewall + on-demand institutional memory.

## Config shape (target)

```yaml
default_backend: gemini

planner_backend: gemini
architect_backend: gemini
service_planner_backend: gemini
auth_planner_backend: gemini

# Per-service coder backend (lets you mix providers per service)
coder_backend_per_service:
  backend: claude_cli      # use Claude for the hard service
  frontend: gemini
  worker: gemini

backends:
  gemini:
    planner_model: gemini-flash-top
    coder_models: [gemini-flash-lite, gemini-flash, gemini-flash-top]
  claude_cli:
    command: "claude"
    additional_args: ["--print", "--output-format=json"]
```

## Order of operations

1. **NOW:** ship M1 on Gemini. Validates pipeline shape end-to-end.
2. Ship M2 on Gemini (evolve mode). Validates iteration loop.
3. Refactor for backend pluggability — extract `BackendFactory`
   interface, route through bizniz.yaml. ~4 hours.
4. Build `ClaudeCliClient` (single-call seam). ~4 hours.
5. Build `ClaudeCliCoder` (tool-loop seam). ~1 day.
6. Build `bizniz_mcp` server. ~1 day.
7. Validate Claude CLI backend on property_manager (same project,
   different driver). Compare time, quality, $0 vs Gemini cost.
8. Run trading platform with Claude as default. Real test.

Total post-validation effort: ~2 days for backend pluggability +
~1 day for MCP. Then ongoing.

## What NOT to do

- Don't switch to Anthropic API (paid). Use Claude CLI subprocess (Max
  plan covers it at $0).
- Don't pivot to Claude before validating the pipeline shape works on
  Gemini. The bugs we've fixed today (workspace prefix, 503 escalation,
  ServicePlanner malformed plans, AuthAgent stalls) are LLM-driver bugs,
  not Gemini-specific. Better to find them once on cheap Gemini than
  twice.
- Don't throw away bizniz's deterministic parts. The auth_operator,
  provisioner, contract renderer, sidecars — all keep value regardless
  of LLM driver.
- Don't try to make Claude CLI mimic bizniz's JSON-schema tool-loop
  output. That's fragile. Let Claude be Claude (native tools + MCP).

## Reference: why Architecture C (narrow per-issue + MCP)

| Approach | Hallucination risk | Cross-issue memory | Parallelism |
|---|---|---|---|
| A. Narrow per-issue, no shared context | Low ✓ | None ✗ | Yes ✓ |
| B. One long session per service | High (context drift) | Full ✓ | No ✗ |
| C. Narrow per-issue + MCP-on-demand | Low ✓ | On-demand via DB ✓ | Yes ✓ |

C is the right answer for "fire and forget" production loops AND for
human-driven debug sessions (`/resume` + ask questions about the DB).

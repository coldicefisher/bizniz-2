# Bizniz

Bizniz is a multi-agent AI pipeline that takes a natural-language problem
statement and produces a working, Dockerized, multi-service application —
running code, passing tests, captured API contracts, and a per-run report.

You describe the app ("a recipe box with auth, recipe CRUD, and an admin
view"); the pipeline plans it into milestones, provisions services from
skeleton repos, writes and debugs the code issue-by-issue, reviews its own
work, runs integration tests against the live Docker stack, and repairs
what fails.

## How it works

The pipeline is a sequence of specialized agents, each with a narrow job:

1. **Planner** — decomposes the problem statement into milestones.
2. **Architect** — decomposes each milestone into services and orchestrates
   the full pipeline.
3. **Provisioner** — materializes the project on disk: clones skeleton
   repos (FastAPI, React, Angular, …), emits `docker-compose`, wires
   FusionAuth for identity.
4. **ServicePlanner / Engineer** — analyzes each service, seeds a scaffold,
   and breaks the work into issues.
5. **Coder / Tester / Debugger loop** — implements each issue via an
   agentic tool loop (file discovery, editing, running tests in
   containers), with model-tier escalation on stalls.
6. **QualityEngineer + CodeReviewer** — review coverage and code quality
   after implementation; findings drive iterative repair passes.
7. **HTTPApiTester / WebUITester** — write pytest+httpx and Playwright
   integration tests and run them against the live compose stack;
   **AgenticDebugger** auto-repairs failures.
8. **UX Designer** — screenshots every frontend route, evaluates the design
   with a vision model, and dispatches styling fixes.
9. **Refactorer** — extracts cross-service duplication into shared
   libraries at milestone boundaries.

Every milestone passes through hard gates
(`ENRICH → IMPLEMENT → SMOKE → REVIEW/REPAIR → INTEGRATION → DONE`) before
it counts as shipped. The end artifact is a `~/bizniz_projects/<slug>/`
directory with a running app, tests, `SKELETON.md` contracts, captured
OpenAPI, git tags per milestone, and a run report.

## Pluggable LLM backends

The same orchestrator runs on either the **Gemini API** or the
**Claude Code CLI** (subprocess), selected per-agent per-service via
`bizniz.yaml`. Model routing is by prefix: `claude-*` → Claude,
`gemini-*` → Gemini, otherwise → OpenAI. An MCP server
(`bizniz/mcp_server/`) exposes pipeline context (prior issues, test
output, audit findings, auth contracts) to Claude-backed coders on demand.

## Quick start

Requirements: Python ≥ 3.10, Docker + Docker Compose, and either a Gemini
API key or the `claude` CLI on PATH. Skeleton repos are expected as
siblings (see `CLAUDE.md` → "Where things live").

```bash
# Setup
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # add your API keys (or create .env by hand)

# Plan only (cheap dry-run)
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project my_app --plan-only "<problem statement>"

# Full build
set -a && source .env && set +a \
  && PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
       --project my_app --auto "$(cat examples/prompts/crm.txt)"

# Resume the most recent run
PYTHONPATH=. .venv/bin/python -u examples/v2_build.py \
  --project my_app --resume --auto
```

`examples/v2_build.py` is the canonical entry point. Useful flags:
`--milestone N` (run one milestone), `--phase <name>` (run one phase),
`--use-v5` (latest implement/review pipeline), `--no-decompose`.

Stand a generated app back up after a run:

```bash
docker compose -f ~/bizniz_projects/<slug>/infra/development/docker-compose.yml up -d
```

## Testing

```bash
# Unit tests (functional tests that hit real APIs are excluded by default)
.venv/bin/python -m pytest bizniz/ -q

# Functional tests (need API keys in .env)
.venv/bin/python -m pytest -m functional -q
```

Tests live alongside their modules in `bizniz/<module>/tests/`.

## Repository layout

| Path | What it is |
|---|---|
| `bizniz/` | The orchestration engine (agents, clients, driver, tools) |
| `examples/v2_build.py` | Canonical CLI entry point |
| `examples/prompts/` | Pre-canned problem statements |
| `docs/roadmap.md` | Locked work-item sequence |
| `docs/changes/`, `docs/sessions/` | Session narratives and design docs |
| `tests/e2e/` | End-to-end lifecycle tests |
| `bizniz.yaml` | Per-agent model/backend configuration |
| `CLAUDE.md` | Deep orientation doc (architecture, invariants, commands) |

Key abstractions inside `bizniz/`: `BaseAIClient` (LLM interface with
OpenAI/Claude/Gemini implementations), `BaseAIAgent` (agent base with
history and retries), `BaseWorkspace` (per-service file I/O),
`BaseExecutionEnvironment` (Docker test sandboxes), and `tool_loop`
(the shared agentic conversation loop).

## Status

Active development. The pipeline has completed multi-milestone builds
end-to-end (auth, CRUD, admin surfaces, integration tests, UX review,
refactor passes). See `docs/roadmap.md` for what's shipped and what's
next, and `CLAUDE.md` for the current session state.

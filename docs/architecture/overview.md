# System Architecture Overview

Bizniz is an autonomous code-generation pipeline. A single problem statement enters at the top, and a fully-built, tested, multi-service system comes out the bottom. Every layer is an AI agent or a deterministic helper feeding the next.

## Component map

```
                             ┌──────────────────────┐
   problem statement ───────▶│    Architect     │  decompose → port-alloc → seed
                             │  (architect/)        │  → docker-build → engineer-dispatch
                             └──────────┬───────────┘
                                        │ ServiceDefinition (per service)
                                        ▼
                             ┌──────────────────────┐
                             │     Engineer     │  analyze → architecture-plan →
                             │  (engineer/)         │  scaffold → run_layered
                             └──────────┬───────────┘
                                        │ EngineeringIssue (per layer)
                                        ▼
                             ┌──────────────────────┐
                             │ CodingOrchestrator   │  TDD or CODE_FIRST loop
                             │ (orchestrator/)      │  + stall detection + escalation
                             └──┬───────────┬───────┘
                                │           │
        ┌───────────────────────┼───────┐   │   ┌──────────────────────┐
        ▼                       ▼       │   │   ▼                      │
 ┌─────────────┐        ┌─────────────┐ │   │ ┌─────────────┐          │
 │  Coder  │        │  Tester │ │   │ │ QuickDebugger│  quick   │
 │ (coder/)│        │(tester/)│ │   │ │             │          │
 └──────┬──────┘        └──────┬──────┘ │   │ └─────────────┘          │
        │                      │        │   │ ┌─────────────┐          │
        ▼                      ▼        │   │ │  Agentic    │  deep    │
 ┌────────────────────────────────────┐ │   │ │  Debugger   │          │
 │            Workspace               │ │   │ └─────────────┘          │
 │ (workspace/)  files + .bizniz/db   │ │   │                          │
 └─────────────┬──────────────────────┘ │   │                          │
               │                        │   │                          │
               ▼                        ▼   ▼                          │
        ┌───────────────────────────────────────┐                      │
        │   Execution Environment               │  pytest / jest      │
        │   (environment/)                      │  inside Docker      │
        └─────────────────────┬─────────────────┘                      │
                              │ ExecutionEnvironmentResult              │
                              └──────────────────────────────────────────┘
```

All data flowing on the arrows is a Pydantic model; see [reference/schemas.md](../reference/schemas.md) for the JSON schemas the LLM must produce.

## Layers and responsibilities

| Layer | Module | Responsibility |
|-------|--------|----------------|
| Provider clients | [`bizniz/clients/`](../modules/clients.md) | Talk to OpenAI / Claude / Gemini, return text + token usage |
| Core abstractions | [`bizniz/core/`](../agents/base_ai_agent.md) | `BaseAIAgent`, `BaseAIClient`, shared `Message`/`ResponseFormat` types |
| Architect | [`bizniz/architect/`](../agents/architect.md) | Problem statement → list of services + Docker compose |
| Engineer | [`bizniz/engineer/`](../agents/engineer.md) | Per-service problem statement → architecture plan + issues |
| Orchestrator | [`bizniz/orchestrator/`](../agents/coding_orchestrator.md) | Per-issue: generate, test, repair until passing |
| Code generators | [`bizniz/agents/coder/`](../agents/coder.md), [`bizniz/tester/`](../agents/tester.md) | Single-shot and multi-file code/test generation |
| Debuggers | [`bizniz/agents/debugger/`](../agents/agentic_debugger.md) | Diagnose pytest/jest failures (quick or deep) |
| Workspace | [`bizniz/workspace/`](../modules/workspace.md) | Filesystem abstraction + per-service SQLite DB |
| Project | [`bizniz/project/`](../modules/project.md) | Multi-service project root, infra/development/* |
| DB | [`bizniz/db/`](../modules/db.md) | Unified MySQL/SQLite with project + workspace scopes |
| Environment | [`bizniz/environment/`](../modules/environment.md) | Run pytest/jest, sandboxed Python eval |
| Preflight | [`bizniz/preflight/`](../modules/preflight.md) | Language-aware structural checks before tests run |
| Tools | [`bizniz/tools/`](../modules/tools.md) | `view_file`, `list_directory`, `search_files`, the agent tool loop |
| Languages | [`bizniz/languages/`](../modules/languages.md) | Per-language strategy (test command, file conventions, prompts) |
| Logging | [`bizniz/logging/`](../modules/logging.md) | Structured JSON pipeline logs |

## Key invariants

1. **One workspace per service.** The architect creates `project_root/<workspace_name>/` and hands it to one `Engineer`. Engineers never share workspaces.
2. **All file I/O goes through the workspace.** Agents must not call `open()` directly — use `workspace.read_file` / `workspace.write_file`. This keeps agents portable across local and remote workspaces.
3. **Tests run inside Docker.** Application code never executes on the host. The `DockerPytestEnvironment` and `DockerJestEnvironment` bind-mount the workspace at `/workspace`.
4. **Skeletons are the seeding mechanism.** Where a skeleton matches, the architect seeds a service from a real cloned repo (auth, Docker, tests already in place) rather than generating from scratch. See [skeleton_seeding.md](skeleton_seeding.md).
5. **Issues are the unit of work below the engineer.** Each issue is one orchestrator run, one set of target files, one test pass. The dependency graph between issues drives layer ordering.
6. **Models escalate; they never auto-downgrade.** When the orchestrator stalls, `ModelProgression` walks the configured list from cheapest → most capable. The next attempt for the same issue uses the new model.

## Where to read next

- For the eight-step end-to-end pipeline narrative: [pipeline_sequence.md](../pipeline_sequence.md)
- For how a single issue gets generated/tested/repaired: [agents/coding_orchestrator.md](../agents/coding_orchestrator.md)
- For the new skeleton flow (committed in `5a95f97`): [skeleton_seeding.md](skeleton_seeding.md)

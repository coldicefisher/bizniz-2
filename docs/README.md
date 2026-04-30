# Bizniz Developer Documentation

This is the per-module developer reference for the `bizniz` Python package — an AI auto-engineering system that takes a problem statement, decomposes it into Dockerized services, generates code and tests for each service, and iteratively repairs failures until tests pass.

If you're new here, start with [home.md](home.md) for the system overview, then read [pipeline_sequence.md](pipeline_sequence.md) for the eight-step pipeline. After that, the rest of this directory is organized by concern.

## Architecture

High-level cross-cutting design.

- [architecture/overview.md](architecture/overview.md) — component map and how the agents wire together
- [architecture/data_flow.md](architecture/data_flow.md) — what data each agent passes to the next
- [architecture/architect_provisioner_split.md](architecture/architect_provisioner_split.md) — Architect plans, Provisioner materializes
- [architecture/skeleton_seeding.md](architecture/skeleton_seeding.md) — skeleton-based service seeding
- [architecture/planner.md](architecture/planner.md) — milestone sequencing
- [architecture/evolve_mode.md](architecture/evolve_mode.md) — milestone-driven incremental builds
- [architecture/cost_tracking.md](architecture/cost_tracking.md) — per-call usage capture, pricing, persistence
- [architecture/error_classification.md](architecture/error_classification.md) — collection-error routing + config-aware repair

## Agents

Each AI agent (one page per agent), top-down from coarsest to finest.

- [agents/base_ai_agent.md](agents/base_ai_agent.md) — common base class
- [agents/planner.md](agents/planner.md) — milestone sequencer (top of the stack)
- [agents/architect.md](agents/architect.md) — system decomposer (also runs `evolve()` per milestone)
- [agents/engineer.md](agents/engineer.md) — per-service requirements + architecture planning
- [agents/coding_orchestrator.md](agents/coding_orchestrator.md) — per-issue iterative loop
- [agents/coder.md](agents/coder.md) — code generation (single & multi-file)
- [agents/tester.md](agents/tester.md) — test generation (three modes)
- [agents/quick_debugger.md](agents/quick_debugger.md) — quick one-shot diagnosis
- [agents/agentic_debugger.md](agents/agentic_debugger.md) — iterative tool-use diagnosis

## Module reference

Lower-level modules grouped by concern.

- [modules/clients.md](modules/clients.md) — AI provider clients (OpenAI, Claude, Gemini)
- [modules/config.md](modules/config.md) — `BiznizConfig` + `bizniz.yaml` loader
- [modules/workspace.md](modules/workspace.md) — `BaseWorkspace`, `LocalWorkspace`, `TempWorkspace`
- [modules/project.md](modules/project.md) — `Project` and `ProjectDB`
- [modules/db.md](modules/db.md) — unified `BiznizDB` and project/workspace scopes
- [modules/environment.md](modules/environment.md) — code execution environments
- [modules/preflight.md](modules/preflight.md) — language validators + registry
- [modules/tools.md](modules/tools.md) — discovery tools, schema builder, agentic tool loop
- [modules/languages.md](modules/languages.md) — Python/TypeScript language strategies
- [modules/orchestrator_internals.md](modules/orchestrator_internals.md) — `ModelProgression`, `StallDetector`, strategy, types
- [modules/engineer_internals.md](modules/engineer_internals.md) — `dependency_graph`, `scaffold`, types
- [modules/utils.md](modules/utils.md) — `code_metadata`, JSON cleanup
- [modules/logging.md](modules/logging.md) — `PipelineLogger`

## Reference

Lookup tables and full schemas.

- [reference/config_reference.md](reference/config_reference.md) — every key in `bizniz.yaml` plus env vars
- [reference/skeleton_reference.md](reference/skeleton_reference.md) — every shipped skeleton (framework, port, contents)
- [reference/schemas.md](reference/schemas.md) — every JSON schema returned by the AI agents

## Conventions used in this docs tree

- Absolute paths in cross-links use the relative form (`agents/coder.md`) since the docs site renders this directory tree.
- "Workspace" always means a `BaseWorkspace` subclass — a directory the agents read/write files to.
- "Service" is one container in the system architecture (one workspace + one Docker image).
- "Issue" is one engineering task tracked in `WorkspaceDB` and dispatched to the `CodingOrchestrator`.

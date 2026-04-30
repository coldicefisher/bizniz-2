# 2026-04-30 — Architect/Provisioner split, FusionAuth defaults, cost DB persistence

Three substantive changes shipped to `main` today, each with full tests
and documentation.

## 1. Architect / Provisioner split (commit `6168282`, merged `960b4cb`)

**What changed.** `AutoArchitect` was doing three things in one class:
planning, infra provisioning, and engineer orchestration. Split into:

- `AutoArchitect` (planning) — `decompose()` is now the only AI call.
  `build()` is a thin shell: `decompose → Provisioner.provision →
  dispatch engineers`. The architect prompt no longer emits
  docker-compose YAML.
- `bizniz/provisioner/` (materialization) — new module with:
  - `Provisioner` class (port allocation + cleanup + skeleton seeding +
    template rendering + image builds)
  - `compose_builder.build_compose()` — deterministic YAML from the
    structured plan
  - `env_builder.build_env_file()` — env vars grouped by prefix
  - `docker_builder.build_image()` — subprocess wrapper
  - Templates registry: `postgres`, `redis`, `fusionauth`, plus
    sentinel `__python_app__` / `__typescript_app__` for skeleton-less
    app services

**FusionAuth as default OAuth.** New `FusionAuthTemplate` ships a real
`kickstart.json`: default tenant issuer, application named after the
project slug, admin + user roles, OAuth redirect URLs for both React
(5173) and Angular (4200) frontends, JWT settings (1h access, 30d
refresh), an initial admin user, and a bootstrap API key. The
PostgresTemplate creates a `fusionauth` DB alongside the app DB. The
architect prompt instructs the LLM to add fusionauth + postgres any
time the project has user accounts.

**Schema cleanup.** `AutoArchitectSchema` no longer requires
`docker_compose`; `SystemArchitecture.docker_compose` is optional and
only used for the human-readable `architecture.md` preview.

**Tests.** 74 new unit tests + 2 functional tests against real Gemini.
Functional tests verified the architect plans FusionAuth + postgres
for a CRM problem and the provisioner produces the full kickstart +
init.sql layout.

**Doc:** [`docs/architecture/architect_provisioner_split.md`](../architecture/architect_provisioner_split.md)

---

## 2. Cost DB persistence (commit `dbfa604`)

**What changed.** Wires the in-memory `CostTracker` through to
durable storage. A *job* is one `architect.build()` invocation. Every
AI call gets a row tagged with `(job_id, service_name, issue_id,
phase)` for flexible rollups.

**Schema additions to `ProjectDB`:**

- `jobs` — id (UUID), project_slug, problem_statement, status, started_at,
  finished_at, total_calls, total_input_tokens, total_output_tokens,
  total_cost, metadata_json
- `api_calls` — id, job_id, timestamp, agent, model, service_name,
  issue_id, phase, input_tokens, output_tokens, duration_ms,
  input_cost, output_cost, total_cost, priced. Indexed on job_id,
  issue_id, service_name, model.

**`CostTracker` lifecycle:**

```python
tracker.start_job(project_slug, problem_statement)  # allocates UUID
# (architect's decompose runs here — buffered in memory)
tracker.attach_project_db(project.db)   # flushes buffer, live-persists from now
tracker.set_service("backend")
tracker.set_phase("phase2.gemini-flash")
tracker.set_issue(7)
# (each AI call lands a row)
tracker.finish_job(status="succeeded")  # rolls up totals onto jobs row
```

`AutoArchitect.build()` opens and finishes the job in a try/finally so
even failed runs leave a complete cost record. `AutoEngineer.run_three_phase`
sets the phase tag to `phase1.frame`, `phase2.<model>`, or
`phase3.agentic` so per-phase rollups reflect which tier solved which
ticket.

**Built-in queries:**

```python
project.db.cost_by_issue(job_id=...)
project.db.cost_by_service(job_id=...)
project.db.cost_by_model(job_id=...)
project.db.get_jobs(limit=50)
project.db.get_job(job_id)
```

`job_id=None` runs an all-time rollup across every recorded job for
the project.

**Tests.** 16 new persistence tests covering schema shape, idempotent
`start_job`, full-context records, `finish_job` rollup, all three
rollup queries, buffer-then-flush semantics, double-persist guard, and
the `attach_workspace_db` legacy alias.

**Doc:** [`docs/architecture/cost_tracking.md`](../architecture/cost_tracking.md)

---

## 3. Documentation refresh (this commit)

- `docs/home.md` — rewritten to reflect the four-level pipeline
  (Architect → Provisioner → Engineer → Orchestrator), cost tracking,
  and the data flow per build. Replaced the 2026-03-08 snapshot.
- `docs/pipeline_sequence.md` — rewritten to nine steps including
  Provisioner (Step 2), three-phase strategy (Steps 5–7), and cost
  rollup (Step 8). Config reference table updated.
- `docs/changes/2026-04-30_provisioner_split_and_cost_persistence.md` —
  this file.

---

## Test totals

- 605 non-functional tests pass (was 545 at session start)
- 4 functional tests pass against real Gemini (architect+provisioner
  end-to-end on a CRM problem)
- 16 deselected functional tests (network-dependent, skip when
  GEMINI_API_KEY is not set)

## Updated work plan

| # | Item | Status |
|---|---|---|
| 1 | Skeleton wiring | ✅ DONE |
| 2 | Quick-pass / framing in engineer | ✅ DONE |
| 3 | Bug fixes (3a/3b/3c) | ✅ DONE / hardened |
| 4 | Library docs | ✅ DONE |
| 5 | Cost analysis (in-memory + summary) | ✅ DONE |
| 5b | **Cost analysis DB persistence** | ✅ **DONE 2026-04-30** |
| 6 | Per-run efficiency doc template | ✅ DONE |
| 7 | **Architect/Provisioner split + FusionAuth default** | ✅ **DONE 2026-04-30** |
| future | Planner agent above architect (multi-week milestones) | not started |
| future | Cross-project rollup via `BiznizDB` | not started |
| future | Re-score historical `api_calls` after pricing change | not started |

## Where things stand

The pipeline is end-to-end working with a clean separation of
concerns: planning, materialization, engineering, orchestration. Cost
is observable and persisted. FusionAuth is the default OAuth and ships
with a complete kickstart. The remaining open work is the Planner
agent for multi-week projects — see the architecture conversation
notes in chat history, not yet drafted as a doc.

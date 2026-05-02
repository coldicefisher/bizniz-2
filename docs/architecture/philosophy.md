# Architectural Philosophy

## The two-layer principle

Bizniz is built on a strict separation:

1. **Language-agnostic orchestration** — the pipeline, planners,
   architects, provisioners, cost tracking, milestone management,
   workspace abstractions. This is the majority of the codebase.
   It doesn't know Python from TypeScript.

2. **Language-specific guardrails** — the narrow set of components
   that encode framework and language knowledge. These are the
   leverage points where adding a new language or framework is a
   bounded, predictable effort.

```
┌─────────────────────────────────────────────────────────────┐
│                 Language-Agnostic Core                       │
│                                                             │
│  Planner → Architect → Provisioner → Engineer → Orchestrator│
│  Workspace → Cost Tracker → Project DB → Run Reports        │
│  Integration Runner → Debug Loop → Tool Loop                │
│                                                             │
│  (Does not change when you add a new language)              │
└──────────────────────────┬──────────────────────────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
    ┌────▼────┐      ┌────▼────┐      ┌────▼────┐
    │ Python  │      │  Type-  │      │ Future  │
    │         │      │ Script  │      │ (Rust,  │
    │Skeleton │      │Skeleton │      │  Go,    │
    │Preflight│      │Preflight│      │  etc.)  │
    │Test Env │      │Test Env │      │         │
    │Prompts  │      │Prompts  │      │         │
    └─────────┘      └─────────┘      └─────────┘
```

To add a new language, you implement five components:

| Component | What it does | Example (Python) |
|---|---|---|
| Skeleton | Working starter project with auth, Docker, tests | `bizniz-skeleton-fastapi` |
| Preflight validator | Static import/symbol validation via AST | `PythonPreflightValidator` |
| Test environment | Docker container that runs the language's test runner | `DockerPytestEnvironment` |
| Coder/tester prompts | Language-specific rules, idioms, docstring requirements | `_GENERATE_MULTI_SYSTEM_PROMPT_PYTHON` |
| Scaffold generator | Stub files in the language's conventions | `.py` stubs + `__init__.py` |

Everything else — the pipeline, the debug loops, the cost tracker,
the integration runner, the import tools, the milestone management —
works unchanged.

## Priority ordering

Every engineering decision is evaluated against this priority stack,
in order:

### 1. Code quality over cost

A $3 run that produces working, well-structured code with proper
docstrings and passing integration tests is worth more than a $0.50
run that produces fragile code the human has to rewrite. The pipeline
optimizes for first-time correctness, not minimum token spend.

This shows up in:
- **Mandatory docstrings** — costs a few extra tokens per function,
  but makes `search_imports` useful and makes the code maintainable.
- **Skeleton-based seeding** — the AI extends proven code instead of
  generating auth from scratch. Higher quality, actually cheaper.
- **FusionAuth over hand-rolled JWT** — production-grade auth as
  infrastructure, not AI-generated crypto.
- **Three-phase strategy** — cheap framing pass populates the
  workspace, then test-and-repair catches real bugs. The framing pass
  costs pennies and prevents the expensive repair pass from starting
  with empty stubs.

### 2. Fewer errors through guardrails

Every guardrail exists because we measured its absence costing real
time and money. Guardrails are deterministic code that prevents
classes of AI errors, not restrictions on what the AI can do.

| Guardrail | Error it prevents | Cost of absence |
|---|---|---|
| Preflight import validation | Wrong import paths survive to test time | 2-4 wasted repair iterations ($0.50-2.00) |
| "Did you mean?" suggestions | Repair LLM guesses same wrong path | Infinite loop until model escalation |
| Stack validation | Engineering against broken infrastructure | 15-30 min of wasted AI cost |
| Image rebuild before integration | Stale container serves old code | Debugger's fixes never take effect |
| Container rebuild on repair | New deps not installed after fix | Same test failure after correct fix |
| Skeleton SKELETON.md | AI rewrites skeleton-shipped files | Silent breakage of auto-discovery |
| Case normalization | "TypeScript" != "typescript" | Wrong test environment, wrong template |
| Workspace file filtering | 527 files in repair prompt | Bloated context, diluted signal |
| Manifest inclusion | Debugger doesn't know what's installed | Wasted turns on pip install |

### 3. Speed

Speed is measured in wall-clock time to a working milestone, not
in individual API call latency. The pipeline optimizes for:

- **Parallel service engineering** — backend and frontend can build
  simultaneously when they don't depend on each other.
- **Layered generation** — issues within a service are ordered by
  dependency, so downstream code imports working code from upstream.
- **Phase 1 framing on cheapest tier** — populate the workspace with
  real code for pennies before running expensive test cycles.
- **Fresh Docker containers per test** — `docker run --rm` avoids
  stale state without restart overhead.
- **Contract capture between layers** — backends publish OpenAPI
  specs so frontends don't guess endpoint shapes.

### 4. Cost effectiveness

Cost is the last priority, not the first. But given the quality,
error, and speed constraints above, the pipeline is highly
cost-conscious:

- **Model escalation** — start on the cheapest tier (gemini-flash-lite
  at $0.001/call), escalate only when tests fail.
- **Preflight prevents wasted test cycles** — catching a bad import
  before running Docker saves the entire test + repair cost.
- **Stack validation prevents wasted engineering** — proving the
  stack runs before spending $1-3 on code generation.
- **Per-call cost attribution** — every AI call is tagged with
  service, phase, issue, and model. The run report shows exactly
  where money went.
- **Milestone-scoped engineering** — `problem_slice` keeps the issue
  list tight. The engineer analyzes one milestone's scope, not the
  whole project.

## The guardrail lifecycle

Guardrails follow a pattern:

1. **Observe a failure class** — e.g., "the AI keeps writing
   `from app.api.deps import ...` instead of `from app.core.auth`."
2. **Measure the cost** — this caused 4 wasted iterations, model
   escalation, $0.77 in debugger calls, 8 minutes of wall time.
3. **Build a deterministic fix** — preflight AST validation +
   fuzzy matching + hint injection into repair prompt.
4. **Verify the fix** — re-run the same scenario, confirm 1-iteration
   fix at $0.05.
5. **Document the invariant** — add to CLAUDE.md "what NOT to do"
   and pipeline_sequence.md.

This is how the pipeline gets better: not by making the AI smarter,
but by making the environment around the AI more informative. The
AI's capability is fixed per model tier. The guardrails' capability
compounds with every failure we observe.

## What this means for new contributors

- **Don't add AI where determinism works.** If you can solve it with
  AST parsing, file system checks, or string matching, do that
  instead of making an AI call.
- **Don't add language-specific code to the core.** If your change
  only applies to Python, it belongs in the Python preflight
  validator, the Python coder prompt, or the FastAPI skeleton.
- **Measure before building.** Every guardrail should have a
  "this cost us X in run Y" justification. Don't add speculative
  guardrails.
- **Document the invariant.** If you add a guardrail, add the
  corresponding "what NOT to do" entry so future contributors
  don't remove it without understanding why it exists.

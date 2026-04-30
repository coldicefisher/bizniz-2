# Cost tracking

Every AI call the pipeline makes is recorded with token counts and a USD
cost estimate. Use this to compare runs and track efficiency over time.

## Module: `bizniz/cost/`

```
bizniz/cost/
├── __init__.py        — public API: get_tracker, price_call, MODEL_PRICING
├── pricing.py         — hardcoded MODEL_PRICING table + price_call()
├── tracker.py         — CostTracker class + global singleton
└── tests/
    ├── test_pricing.py
    └── test_tracker.py
```

## Flow

```
┌──────────────┐  call API   ┌───────────────┐
│  AI Client   │ ──────────▶ │ Provider SDK  │
│ (Gemini /    │ ◀────────── │ (response +   │
│  OpenAI /    │  usage      │  usage data)  │
│  Claude)     │             └───────────────┘
└──────┬───────┘
       │ get_tracker().record(
       │   agent, model,
       │   input_tokens, output_tokens,
       │   duration_ms,
       │ )
       ▼
┌─────────────────────────────────────────┐
│ Module-level CostTracker singleton      │
│  - in-memory CallRecord list            │
│  - thread-safe                          │
│  - optional workspace DB persistence    │
└──────┬──────────────────────────────────┘
       │ tracker.summary()
       ▼
┌─────────────────────────────────────────┐
│ CostSummary                              │
│   total cost / tokens                    │
│   by_model: {model: {calls, in, out, $}} │
│   by_agent: {agent: {calls, $}}          │
│   unpriced_models  (warn if any)         │
└─────────────────────────────────────────┘
```

## Capture in clients

Each client wraps its API call and forwards usage to the tracker:

| Client | Provider field | Records on |
|---|---|---|
| `GeminiClient` | `response.usage_metadata.prompt_token_count` / `.candidates_token_count` | every `get_text()` |
| `OpenAI` (chat completions / responses) | `completion.usage.input_tokens` / `.output_tokens` | every `get_text()` |
| `ClaudeClient` | `stream.get_final_message().usage.input_tokens` / `.output_tokens` | every `get_text()` |

The agent name is taken from `client._caller_agent`, which `BaseAIAgent`
sets to the agent class name in lowercase (`coder`, `tester`,
`engineer`, `architect`, `agentic_debugger`, …) at construction
time.

## Pricing table

`MODEL_PRICING` is a dict of canonical model name → `{input, output}`
USD per 1,000,000 tokens. `resolve_model()` maps short aliases used by
`BiznizConfig` (`gemini-flash-lite`, `gemini-flash`, `gemini-pro`,
`claude-sonnet`, `claude-opus`, …) to their real pricing keys.

If a model isn't in the table, `price_call()` returns a `CallCost` with
`priced=False` and `total_cost=0.0`. The summary surfaces these as a
warning so we know to add the entry.

## Public API

```python
from bizniz.cost import get_tracker, price_call, MODEL_PRICING

# Read after a run
summary = get_tracker().summary()
print(summary.format())
# calls=42  input=128,440  output=53,920  total=$0.1832
#   by model:
#     gemini-2.5-flash-lite                  calls= 38  in=    98,000  out=    21,200  $0.0182
#     gemini-3.1-flash-lite-preview          calls=  3  in=    19,440  out=    20,720  $0.0102
#     gemini-3.1-pro-preview                 calls=  1  in=    11,000  out=    12,000  $0.1338
#   by agent:
#     coder                  calls= 22  $0.1402
#     tester                 calls= 12  $0.0238
#     architect             calls=  1  $0.0009
#     engineer              calls=  7  $0.0183

# Score an arbitrary call
cost = price_call("gpt-4o", input_tokens=1000, output_tokens=500)
# CallCost(input_cost=0.0025, output_cost=0.005, total_cost=0.0075,
#          model="gpt-4o", priced=True)

# Reset between runs (e.g. in a long-lived REPL)
get_tracker().reset()
```

## Run-end summary

`examples/architect.py` prints the `CostSummary` after each end-to-end
run. The per-run efficiency docs under `docs/runs/` should include the
formatted summary so we can compare across runs.

## Persistence — jobs and api_calls

A *job* is one `architect.build()` invocation (or any other top-level
unit of work). Each AI call is tagged with a `job_id`, plus
`service_name`, `issue_id`, and `phase` for fine-grained rollups.

Two tables in `ProjectDB` (at `<project_root>/.bizniz/project.db`):

```sql
jobs
  id                  TEXT PRIMARY KEY    -- UUID
  project_slug        TEXT NOT NULL
  problem_statement   TEXT
  status              TEXT (running | succeeded | failed | cancelled)
  started_at          TEXT NOT NULL
  finished_at         TEXT
  total_calls         INTEGER
  total_input_tokens  INTEGER
  total_output_tokens INTEGER
  total_cost          REAL
  metadata_json       TEXT

api_calls
  id, timestamp, job_id, agent, model, service_name, issue_id, phase,
  input_tokens, output_tokens, duration_ms,
  input_cost, output_cost, total_cost, priced
  -- Indexes on job_id, issue_id, service_name, model
```

### Lifecycle

`Architect.build()` opens the job and finishes it for you:

```python
tracker = get_tracker()
job_id = tracker.start_job(project_slug, problem_statement)
# (architect's decompose call records here, buffered in memory because
#  the project DB doesn't exist yet)

provisioner.provision(...)        # creates project_root/.bizniz/project.db
tracker.attach_project_db(project.db)
project.db.start_job(job_id, project_slug, problem_statement)
# (buffered records flush; subsequent calls live-persist)

# Inside engineer dispatch:
tracker.set_service("backend")
tracker.set_phase("phase1.frame")
# ... AI calls ...
tracker.set_phase("phase2.gemini-flash")
tracker.set_issue(7)
# ... AI calls ...

tracker.finish_job(status="succeeded")  # rolls up totals onto the jobs row
```

If `start_job` runs before any DB exists (the architect's first
`decompose()` call), records buffer in memory and flush on
`attach_project_db()`. After attach, every `record()` writes a row
immediately. Errors during persistence are swallowed so a tracking
glitch never breaks a real call.

### Built-in rollup queries

`ProjectDB` exposes:

| Method | Returns |
|---|---|
| `get_jobs(limit)` | recent jobs ordered by `started_at` |
| `get_job(job_id)` | one job row |
| `cost_by_issue(job_id=None)` | per-issue calls + tokens + cost |
| `cost_by_service(job_id=None)` | per-service calls + cost |
| `cost_by_model(job_id=None)` | per-model calls + tokens + cost |

Pass `job_id=None` for an all-time rollup across every run of the
project; pass a UUID to scope to one run. Sample rollup query for
"which issues cost the most across all time":

```python
for row in project.db.cost_by_issue():
    print(row["issue_id"], row["calls"], row["total_cost"])
```

## Limitations / future work

- **Pricing is provider list price, not actual billed.** Volume discounts,
  cache discounts, and prompt-caching reductions aren't modeled.
- **No retroactive re-scoring.** If you change `MODEL_PRICING`, prior
  rows retain the cost computed at record time. Walk `api_calls` and
  re-`price_call()` if you need to re-score historical data.
- **Cross-project rollup not built in.** Each project DB sees only its
  own jobs. The unified `BiznizDB` (single MySQL/SQLite store) is the
  path to a cross-project view; not wired into the cost path yet.

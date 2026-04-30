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
sets to the agent class name in lowercase (`autocoder`, `autotester`,
`auto_engineer`, `auto_architect`, `agentic_debugger`, …) at construction
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
#     autocoder                  calls= 22  $0.1402
#     autotester                 calls= 12  $0.0238
#     auto_architect             calls=  1  $0.0009
#     auto_engineer              calls=  7  $0.0183

# Score an arbitrary call
cost = price_call("gpt-4o", input_tokens=1000, output_tokens=500)
# CallCost(input_cost=0.0025, output_cost=0.005, total_cost=0.0075,
#          model="gpt-4o", priced=True)

# Reset between runs (e.g. in a long-lived REPL)
get_tracker().reset()
```

## Run-end summary

`examples/auto_architect.py` prints the `CostSummary` after each end-to-end
run. The per-run efficiency docs under `docs/runs/` should include the
formatted summary so we can compare across runs.

## Limitations / future work

- **No DB persistence yet.** `CostTracker.attach_workspace_db()` is wired
  but the `WorkspaceDB.save_api_call()` method has not been added. Once it
  ships, every `record()` call lands a row in `api_calls` (per workspace)
  for cross-run analysis.
- **Pricing is provider list price, not actual billed.** Volume discounts,
  cache discounts, and prompt-caching reductions aren't modeled.
- **No retroactive scoring.** If you change `MODEL_PRICING`, prior records
  retain the cost computed at record time. Re-running `summary()` re-prices
  using the current table only because cost is computed inside `record()`
  and cached on the `CallRecord`. To re-score, write a small helper that
  walks `tracker.records()` and calls `price_call()` again.
